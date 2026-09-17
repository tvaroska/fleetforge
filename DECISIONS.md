# Decisions

Append-only log of product/technical decisions and learnings. Newest first.
Each entry: what was decided, why, and where the details live. Never rewrite
history — supersede an old decision with a new entry that references it.

---

## 2026-09-17 — `fw_version` is the version that BOOTED, and there is now exactly one way to say it

**R1-fw-2.** The behaviour was already right; nothing kept it right.

**1. The fix was a seam, not a behaviour change.** `esp_app_get_description()` already
returned the *running* image's descriptor — the new slot's after an OTA, the old slot's
again after a rollback — but two call sites read it and a third plausible source existed:
`ff_ota_cmd_t::version`, the version the server asked us to install. Reporting that is
correct on every deploy that worked and wrong on every deploy that did not, i.e. silent
exactly when the fleet needs the field. So `ff_identity_fw_version()` is now the only way a
version reaches the wire, and three tripwires in `tests/test_ff_cfg.py` pin it: one source,
both payloads, and `ff_ota.c` neither emitting `"fw_version"` nor including `ff_identity`.
Tripwires rather than a runtime check because no host test can execute this code and the
fix would ship by OTA to a board whose OTA reporting is what is broken.

**2. The boot line now names the image state.** `running image: fw_version 0.3.2, ota state
pending_verify` is what distinguishes an applied update from a rolled-back one in a serial
log with no server attached. It is read-only (`esp_ota_get_state_partition()` and nothing
else) and deliberately **not** merged with `ff_mqtt.c`'s reader of the same otadata: two
small readers of one state is the cheap outcome, one shared helper that someone later
improves is a bricked fleet.

**3. No cross-reboot state.** Still nothing persisted — the announce from the image that
booted is the whole report. Restates R1-fw-1 §1; supersedes nothing.

**4. T2 ran the negative before the positive**, because the negative needs the board still
on the old image and is the half nobody checks: told `0.3.2`, failed on a corrupted digest,
and `up/hb` plus `devices.fw_version` still read `0.3.1` afterwards. Only then the applied
update, `0.3.1 → 0.3.2`.

**5. `apply: "on_command"` is how you drive a QEMU acceptance run**, not a race against
`esp_restart()`. R1-fw-1's workaround (poll for `staged and bootable`, kill before the
reboot) has a **40 ms** window and loses: the board soft-resets, hits the emulator's known
`esp_timer_impl_init` panic, and the bootloader retires the `PENDING_VERIFY` image exactly
as designed — leaving the old slot running and `otadata` `aborted`, which reads like a
firmware fault and is not one. Staging without applying and then power-cycling removes the
race instead of trying to win it (`docs/runbooks/agent-qemu.md`).

`agent/version.txt` → `0.3.1`. `spec/device-protocol.md`'s example `agent_version` string
is now stale — a proposal only; `spec/` is protected.

---

## 2026-09-17 — The agent applies an update: verified against flash, undone on mismatch, and finished at `rebooting`

**R1-fw-1.** `ff_ota.c` is the first code in this product that moves a boot partition, so
the decisions are mostly about what it refuses to do.

**1. R1 stops at `rebooting`, and nothing is persisted across the reboot.** The agent
publishes `staging → downloading → verifying → staged → applying → rebooting` and calls
`esp_restart()`. No `confirming`/`confirmed`, no `cmd_id` in NVS: the image that comes back
announces itself and that announce is the whole report. Half a cross-reboot state machine
— a stored `cmd_id` nobody drives to a terminal state — is worse than none. R2-fw-3 owns
the confirm timer as a shipped feature; the confirm/rollback pair that has been dormant in
`ff_mqtt.c` since R0 goes **live** as a consequence of this task and was deliberately left
untouched (verified in T2: the OTA'd image logged `CONFIRMED` after its announce PUBACK).

**2. The sha256 is taken by reading the partition back, after `esp_https_ota_finish()`,
and a mismatch restores the boot partition.** Hashing the stream as it arrives is the
obvious design and it is wrong twice over: `esp_ota_write` withholds the image header's
first 16 bytes until the write completes, so the stream hash covers bytes that were never
on flash, and it cannot detect a bad flash write — which is the failure that matters. The
undo (`esp_ota_set_boot_partition(esp_ota_get_running_partition())`) is mandatory because
`finish()` has *already* switched the boot pointer by the time we can hash: without it a
board with a rejected image boots into it at the next power cut, and for this product that
is a van and a screwdriver. Proven in T2 with a hand-published `stage` carrying a corrupted
digest: `boot partition put back to ota_1`, `failed`/`sha256 mismatch`, and a cold restart
still on the old image.

**3. The OTA runs on its own task.** A QoS-1 publish from the esp-mqtt event handler
deadlocks the client, and a multi-minute download inside the handler stops the keepalive.
One task, `s_running` as a flag rather than a mutex: a second `stage` while one runs is
**refused and reported** `failed` on the new `cmd_id`, never queued — two writers to one
slot corrupt it, and a deploy silently waiting behind another is a deploy the server cannot
explain. Duplicate `dn/cmd` ids are dropped by the pre-existing dedup, not by this task.

**4. `CONFIG_ESP_HTTPS_OTA_ALLOW_HTTP` was NOT added** (the plan called for it; this is the
deviation). Read in the pinned image's sources: `esp_https_ota` gates on
`is_server_verification_enabled()`, which is true whenever `crt_bundle_attach` is set — and
we set it, for the same Mozilla bundle `ff_enroll.c` uses. A plaintext `http://` URL never
reaches the TLS layer, so the option is inert for us. It is also a line in
`agent/sdkconfig.defaults`, a CRITICAL flash-time file, that would have relaxed a security
posture to no effect. The QEMU lab downloads over plain HTTP today with the option absent.

**5. The agent resolves the artifact link's 307 itself.** IDF v5.5.5 rebuilds the `Host`
header wrong on a redirect — `esp_http_client_init()` uses `_get_host_header(host, port)`,
while `esp_http_client_set_url()` sets the bare host and only when the host *string*
changed — and an S3-compatible presigned URL signs `host`. Redirect a board from
`:8080` to an object store on `:9000` and it presents a Host that was never signed:
**403 SignatureDoesNotMatch**, reported by IDF as "File not found(403)". So
`resolve_artifact_url()` does one header-only `GET` with `disable_auto_redirect`, captures
`Location` from `HTTP_EVENT_ON_HEADER`, and hands the final URL to `esp_https_ota_begin()`.
Production (GCS on :443) never hit this; a self-hosted MinIO — V2's entire shape — fails
every deploy. The cost is one extra round trip whose body is empty anyway.

**6. The board announces `capabilities: ["ota"]`,** because `POST /v1/devices/{id}/deploy`
answers 409 without it. It stays as short as the truth: it was `[]` at R0 for the same
reason.

**7. `confirm_timeout_s` is parsed and ignored at R1** (a WARNING if it differs from the
firmware's own `CONFIRM_TIMEOUT_S`), and `artifact.sig` is parsed-and-ignored:
`spec/device-protocol.md` lists it, `broker/commands.py::stage_payload()` never emits it.
**Spec proposal, not applied** (`spec/` is protected): either the spec drops `sig` or R2
implements it.

**8. `apply: "on_command"` stages and stops at `staged`,** not at `awaiting_safe_window`.
An always-on agent has no window to wait for, so reporting one would be a state nothing
ever leaves. R1 ships no `apply` command, so the board simply waits where the simulator
waits.

**Known limitation of the lab, not of the firmware:** QEMU panics on the boot that follows
`esp_restart()`, inside IDF's `esp_timer_impl_init → esp_intr_alloc`, before `app_main`,
in whichever image it lands on — including the pre-OTA `0.3.0` that boots fine from
power-on. A peripheral interrupt survives the soft reset that the CPU does not. The OTA'd
image was proven to boot, announce `0.3.1` and confirm itself by cold-starting the
emulator instead; `docs/runbooks/agent-qemu.md` has the decoded backtrace and the recipe.

Details: `docs/features/ota-deploy.md` → *The device half (R1-fw-1)*;
`.claude/plans/R1-fw-1-esp-https-ota-update-command.md`.

---

## 2026-09-17 — Every device-reported deploy state is a row, recorded once, and the server still authors no cancel

**R1-be-4.** `up/status` now writes `deploy_events` through the table's one writer,
`deploys.record_observed_status`. Five decisions.

**1. The retained topic forces deduplication on `(device_id, cmd_id, state)`.**
`up/status` is retained and the ingestor re-`subscribe`s on every connect, so a broker
blip, a container restart or a stack deploy replays the last status of every board.
Retained status is nevertheless **ingested** — unlike telemetry and log, which are
dropped when retained — because the retained value is exactly how an outcome published
while the ingestor was down is delivered at all, and there is no manual ack: a dropped
status is an outcome lost forever. So the duplicate is handled in the writer, not by
dropping the message. It is a **writer rule, not a unique index**: a repeated state is
legal data (`downloading → failed → downloading` inside one retry), so the database must
still accept it. The deliberate consequence is that a repeated state inside one
transaction collapses to its first occurrence and the stored `pct` is the first one seen
— `deploy_events` is a log of transitions, not a progress feed.

**2. A replay proves nothing about liveness.** A retained status records its state but
does not move `last_seen`, so an ingestor restart cannot mark a dead fleet alive. It
still resolves the device through `store.fetch_live_device` (the old private `_fetch`,
now public), which keeps **one** drop path for the unregistered/decommissioned case:
`deploy_events.device_id` is an FK with `ON DELETE RESTRICT`, and an insert for an
unknown device would raise `IntegrityError` that the message loop swallows as "ingest
failed", losing the write.

**3. The wire may not author `requested`, and an unmapped `cmd_id` is recorded anyway.**
A board reporting the server's own state is a firmware bug or a forgery: WARNING, no
row. Conversely a `cmd_id` with no `requested` row — a transaction from before a
database rebuild, a command from another server — **is** recorded, with both version
columns NULL and an INFO line. "Every outcome" means every outcome, including the ones
we cannot explain. Everything else is copied off the `requested` row, which is the whole
reason R1-be-2 invented that state: without `artifact_version` on the terminal row, R5's
delivery-success KPI is uncomputable.

**4. There is no server-authored cancel, and TODO's "cancel" is the device's rollback.**
R1 ships no `cancel` command — `broker/commands.py` publishes `stage` only. So: success
→ `confirmed`; failure → device-reported `failed` (plus the server's `publish_failed`);
cancel/abandon → the device's own `rolled_back` or `failed`, because the device owns the
reboot and the rollback. When a new deploy supersedes an in-flight one the server writes
**nothing**: that would be authoring a state for a command the device provably did
receive, which the 2026-09-17 R1-be-2 entry forbids. The superseded intent stays an open
transaction with no terminal event — which is precisely what "fleet safety loss" means
in R5, and is the truthful record. Do not "fix" this.

**5. Device-reported `detail` is sanitised before it is stored forever.** Control
characters stripped (a device that can inject a newline can forge a log line — the
`api/schemas.py::_printable_detail` precedent), truncated to 200 chars
(`MAX_PROGRESS_DETAIL`, restated locally because `deploys.py` is transport-agnostic and
must not import the FastAPI side), and `https?://\S+` redacted to `<url>` — the signed
download URL is a bearer credential, our agent does not echo it but a third party's
might, and R1-be-3's evidence asserts no stored `detail` contains `http`. Kept true by
construction. The wire model coerces rather than raises (`pct` that is not an int in
0..100 → dropped, a non-string `detail` → `str(...)`): a `ValidationError` on a
*retained* topic loses the same outcome on every reconnect.

`EventType.DEVICE_DEPLOY` is emitted only when a row was actually written — a deduped
replay is not news — and the SSE envelope gains no field: the consumer re-reads.

---

## 2026-09-17 — Firmware downloads go through our own signed URL, and the API redirects rather than proxies

**R1-be-3.** The device is handed a link on **our** origin —
`GET /v1/artifact/{sha256}/bin?exp=…&sig=…`, the shape `spec/device-protocol.md` already
fixed — instead of the object store's presigned URL. Six decisions.

**1. The signature is the authorization, and it is ours.** One HMAC-SHA256 over
`v1\n<sha256>\n<exp>`, keyed by `ARTIFACT_URL_SECRET`, minted in
`api/routers/deploys.py` and verified in `api/routers/artifact_download.py`
(`src/fleetforge/artifact_urls.py` is both). This is the **second unauthenticated
endpoint** after `POST /v1/enroll`: a board has no admin token and never will. What it
buys over handing out the store's URL: the URL shape and the authorization are backend-
independent (S3 today, GCS in prod, neither visible to the board), the store URL's short
life is decoupled from the command's life, and rotating the secret kills every link in
flight — at most `SIGNED_URL_TTL_S` of staged deploys, which simply re-deploy. There is
deliberately **no key rollover**; one key, one rotation story.

*The three rules a reviewer should check, each a way this is normally wrong:*
`hmac.compare_digest`, never `==`; the MAC is verified **before** `exp` and **before**
any store call, so "expired" vs "forged" is not an oracle and an anonymous caller cannot
drive an IAM `signBlob` call; the digest is **rejected, never normalised**, and `exp` has
exactly one spelling (digits, no leading zero, no sign, no underscores — `int()` accepts
three of those four).

**2. 307 redirect, not a proxy.** `design/production.md` promises artifacts are "served
without touching the API process". A proxy would need an HTTP client in the production
image (`httpx` is a dev dependency) and would hold a uvicorn threadpool slot per board
for a 1.9 MB transfer on a 256 M container. It also means `Range`, `Content-Range`,
suffix ranges and 416 are the **store's** RFC-correct implementation rather than a
hand-rolled parser on the OTA critical path — which matters because R5 resume is exactly
a `Range:` request. Verified for real against MinIO in
`tests/test_artifact_download_minio.py`.

**3. One upstream signature per artifact per cache lifetime.** `storage/urlcache.py`
(`SignedUrlCache`, modelled on `CatalogCache`) is now the **only** module in the app that
calls `ObjectStore.signed_url` — a tripwire in `tests/test_api_deploy.py` holds it there.
A URL is reused only while `monotonic() < signed_at + ttl - ARTIFACT_URL_REFRESH_MARGIN_S`
(default 300 s), so no board is ever handed a link that dies mid-transfer; failures are
never cached. Measured: ten ranged downloads of one artifact cost one signing call.

**4. The download path touches no database.** A download keeps working while Postgres is
degraded, and the signature already carries the authorization — membership in `artifacts`
tells a signature-holder nothing new. A validly signed digest with no object behind it
ends as the store's own 404 after the redirect. A malformed digest is **404, never 422**:
a schema-error body is an oracle, and the only useful answer to an unsigned caller is one
uniform "no".

**5. A deploy no longer fails fast when the object store is unreachable — deliberate.**
`POST /v1/devices/{id}/deploy` used to sign through the store and answer 503 when that
failed. It now mints locally and never touches the store, so that check is gone: nothing
in the four-verb seam can test existence cheaply (`get` downloads the whole image), the
`artifacts` row is already the evidence the bytes were stored, and the download endpoint
answers the outage honestly at the moment it is true. In exchange the deploy path gained
a **503 when `ARTIFACT_URL_SECRET` or `PUBLIC_BASE_URL` is unset** — a 202 carrying a URL
no board can redeem is the lie `NullCommandPublisher` refuses to tell.

**6. Residual, recorded rather than fixed: uvicorn's access log prints the signature.**
The application never logs a URL, a signature or the secret (asserted in three suites),
but the access line contains the full request target, so anyone who can read container
logs can replay a link for its remaining life. Acceptable at v1 — reading the logs
already implies host access, and the link expires — and the fix (a `--access-log`
formatter that strips the query) belongs with the observability work, not here.

**Production prerequisite — filed, not applied.** `services/prod/.env` needs a fresh
`ARTIFACT_URL_SECRET` (32-byte hex, `just artifact-secret`) and
`PUBLIC_BASE_URL=https://bingo.tvaroska.sk`, and the prod compose must pass both to the
`api` service, **before the next prod deploy** — otherwise every deploy answers 503.
`services/` is a different repo and root `CLAUDE.md` forbids changing prod env without
asking, so this is written down in `docs/features/ota-deploy.md` the way R1-be-2 filed
its `MQTT_COMMAND_*` prerequisite.

---

## 2026-09-17 — The API commands over MQTT as its own credential, and a retry is the same transaction

**R1-be-2.** Four decisions, all about who may say what and what gets written down.

**1. A fourth broker credential, `commander`, that can only send on `dn/`.**
`mosquitto/bootstrap.sh` creates a dynsec role with exactly one ACL —
`publishClientSend 'ff/v1/d/+/dn/#' allow` — and one client holding it. Not the dynsec
admin, which is broker-root over `$CONTROL` and needs none of this to publish a deploy;
not the ingestor, which must stay read-only. The role has **no** `subscribePattern` and
**no** `publishClientReceive`, so a leaked deploy credential cannot forge `up/status` or
read the fleet, and the ingestor stays the only subscriber. The `device` role is still
empty — the fleet's authz is the two `%u` pattern rules in `mosquitto/acl`, and adding a
`+` rule to `device` to "make commands work" would be a confidentiality breach. It does
work because **both ACL backends are consulted and allow wins**: the pattern file's
`pattern read ff/v1/d/%u/dn/#` grants delivery over dynsec's default receive-deny. That
is now asserted live by `just broker-check` (`_check_command_delivery`: A receives, B
does not, and the commander is dropped on `up/`).

*Gotcha, and it cost the most time to learn in R0:* MQTT 3.1.1 has **no deny feedback**.
A refused publish looks exactly like a delivered one — no PUBACK reason code, no error.
Every negative broker assertion has to be "nothing arrived at a subscriber", never "the
publish raised". Reason codes exist only over MQTT 5 from inside the container.

**2. `requested` is a server-authored state, and the only one.** It records *"we
published a command"*, which no device can report; without it an abandoned deploy is
invisible to the KPIs and R1-be-4 cannot map an incoming `cmd_id` to the version that was
intended. The single exception is the one **terminal** state the server may write:
`failed` with `detail={"reason": "publish_failed"}`, honest because the broker refused, so
the board provably never saw the command and the transaction the `requested` row opened is
closed rather than left open forever. Beyond that the server never writes a state for a
transaction the device did receive, and **never expires `awaiting_safe_window`** — a
vehicle in motion may sit there indefinitely (`design/architecture.md` principle 5). There
is no sweeper, no timeout task and no `asyncio.sleep` on this path, and
`tests/test_api_deploy.py` asserts their absence in the source.

**3. A retry inside the URL's TTL is the same transaction, and writes no second row.**
The device deduplicates on the command `id`, so a retried `stage` must carry the id the
first attempt used or the board downloads the same firmware twice. A POST for
`(device_id, sha256)` matching the newest `requested` row for that device — younger than
`SIGNED_URL_TTL_S` and with no terminal event — reuses that row's `cmd_id`, signs a
**fresh** URL, republishes, and answers `reused: true`. A different artifact is always a
new intent. The TTL bound is what makes it safe: past it the first URL has expired, so a
board that never acted on the first command cannot act on it now.

**4. The signed URL is a bearer credential and is never persisted.** It is not in the 202
body, not in `deploy_events.detail` (which carries only sha256/size/target/apply), and not
in any log line — the publisher logs `id` and `type` only. One URL is signed per accepted
deploy; signing is an IAM round trip on GCS since S0-infra-5, so it is not free.

`deploy_events` now has exactly one writer, `src/fleetforge/deploys.py`, enforced by a
tripwire in `tests/test_invariants.py`. R1-be-4's `up/status` ingestion adds its writer
there rather than growing SQL in `ingestor/handlers.py`.

**Production prerequisite:** `services/prod/.env` needs `MQTT_COMMAND_USERNAME` /
`MQTT_COMMAND_PASSWORD` before the next prod deploy, or `mosquitto-init` refuses to start
(`:?` on both) and every `POST /v1/devices/{id}/deploy` answers 503. `services/` is a
different repo and prod env is never edited without asking, so this is filed, not done.

Detail and T2 evidence: `docs/features/ota-deploy.md` → *Deploy orchestration (R1-be-2)*.

---

## 2026-09-16 — Version labels live in their own table, and the artifact size cap is one number

**R1-be-1.** Three decisions, all forced by things that were already frozen.

**1. `artifact_versions`, not a `version` column on `artifacts`.** S0-infra-4 froze
`artifacts` content-addressed — `sha256` is the primary key — and left no `version`
column, while `spec/device-protocol.md` and `spec/prd.md` → *Retention* both need one. A
column could not work: one digest would carry exactly one label, so re-tagging
byte-identical firmware would be a PK collision rather than the ordinary thing it is.
Migration `0004` adds a small mutable label layer over the immutable blobs —
`(target, version)` PK, a real FK to `artifacts.sha256` with `ON DELETE RESTRICT`, and an
index on `(target, created_at)`. Same shape as S0-infra-6's index-as-pointer decision.
`artifacts` is untouched.

*Gotcha for R2's pruner:* `RESTRICT` means the label must be deleted before the blob. That
is the intended order — it is what stops the pruner deleting bytes a release still names —
but a pruner written blob-first will simply fail.

*Gotcha for the next migration:* unlike `0003`, `0004` carries a foreign key. `0003`'s
absence of FKs was forced (PostgreSQL cannot FK into the JSONB `builds.outputs`), not a
project-wide principle. Use one where the column is plain.

**2. Three statuses, because a content-addressed store collapses two success cases.** New
label → 201. Same bytes under the same `(target, version)` → 200 `created: false`, since a
re-`put` of the same key is a no-op by construction. Same label over *different* bytes →
409, label unchanged: a version is a promise about which image it is, and silently
re-pointing it would make every `deploy_events` row that mentions it ambiguous. Re-tagging
the same bytes under a second label is free — both labels name one object.

**3. `prd.md`'s "1.9 MB" and `ota_slot_size` 1966080 are one number, not two.** 1966080 B
is 1.875 MiB, which rounds to 1.9 MB. The task as filed asked for two rejections with two
error messages; implementing that would have produced a second, slightly different cap and
a rejection nobody could explain. The endpoint enforces the authoritative one only —
`firmware/manifest.py::SUPPORTED_LAYOUTS`, already what agent-bundle validation reads, so
an upload and a bundle cannot disagree about how big a slot is. `spec/` is protected during
`/implement`, so the clarification is **proposed** in `spec/open-questions.md`, not applied.

Detail and T2 evidence: `docs/features/ota-deploy.md` → *Artifact upload (R1-be-1)*.

---

## 2026-09-16 — R1 opens while R0 stays open, because R0 is parked and not in progress

`TODO.md` carries Sprint 0 plus **one** release, and from today it carries two. That is a
deliberate exception, not drift, so it is written down rather than left for the next
`/replan` to discover.

- **The rule assumes the active release is being worked on.** R0 is not. Every desk-bound
  task in it is done and archived; all five open tasks need a physical board, and the
  gating one (`S0-fw-3`) needs the board *and* a bench session. "One active release" is a
  focus rule, and there is nothing left to focus on — the alternative to opening R1 is not
  finishing R0 sooner, it is not building anything until hardware appears.
- **R1 was unblocked the day before.** `R1-BE-0` closed on 2026-09-15: the GCS credential
  is an impersonation, `signBlob` is measured rather than assumed, and containment is
  verified through the adapter. R1-be-3's signed-URL delivery therefore rests on a
  mechanism that has been round-tripped, which is what made R1 startable at all.
- **Seven of R1's eight tasks need no board, and that includes the firmware half.** The
  non-obvious part: `docs/runbooks/agent-qemu.md` boots the real unmodified
  `agent/dist/esp32` bundle against the dev stack over the emulated `openeth` NIC, so
  `stage → download → apply → reboot → report-version` is exercisable at a desk. R1's
  firmware work is not hardware work.
- **`R1-test-1` stays bench-gated and keeps its name.** The tempting move is to redefine
  the E2E as "passes in QEMU" and close the release. Refused: QEMU has no radio, no power
  behaviour and no chip revision, and R1's claim is about a board. The same reasoning that
  keeps `R0-test-2` open keeps this one honest.

**What this obliges.** R0 does not lose priority — the bench session runs the moment a
board is in hand, in the order `TODO.md` gives, and `R0-test-2` still closes R0 before R1
can close. When R0 does close, `/replan` archives both R0's and R1's completed tasks and
the file returns to one release. Until then, two release sections coexist and the R0 one
is the one that gets picked up first when hardware exists.

---

## 2026-09-15 — the agent catalog is a pointer object, not a bucket listing (S0-infra-6)

**Closes** the 2026-09-11 entry *agent bundles are artifacts, not image contents*, whose
blocker the S0-infra-5 entry above removed. The application image now ships **zero**
firmware: `COPY agent/dist /app/agent` and `AGENT_IMAGES_DIR` are gone, `just agent-publish
<target>` uploads a verified bundle as content-addressed blobs, and the flasher reads it
back through `ObjectStore`. A firmware fix now reaches boards with no app-image rebuild and
**no restart** — measured at the TTL, same container id.

- **`agent/index.json` is a pointer, not a listing, because `ObjectStore` has four verbs
  and `list` is not one of them.** Adding a fifth verb to serve this was rejected twice
  over: listing is a per-backend paging contract, and a catalog defined as "whatever is in
  the prefix" cannot be rolled back, cannot be published atomically, and answers "what is
  current?" with a guess over lexicographic order. The index is the single mutable key in
  the scheme (`Cache-Control: no-store`, never through `put_blob`); everything else is
  immutable `blobs/sha256/<digest>`. Publish writes blobs first and the index last, so a
  crash leaves unreferenced blobs rather than a catalog pointing at bytes that do not
  exist. **Rollback is therefore one index write** (`just agent-rollback <target> <digest>`)
  against a capped 20-entry `superseded` history — not a rebuild, which is what S0-fw-3 and
  S0-infra-2's three stale bundles each cost.
- **No database table for the catalog.** Agent bundles are per-deployment facts, not
  per-tenant records; a row would have to be kept in step with the bytes by hand, and the
  index already is that state. `firmware_builds`/`firmware_artifacts` stay empty here.
- **A failing refresh never serves the previous snapshot.** `CatalogCache` clears before it
  reads, so a store outage is a named 503 rather than a manifest whose parts the API can no
  longer hand out; failures are not cached either. Three distinct answers, and the 503 text
  is lifted verbatim into the flasher banner, so it carries no bucket, key or traceback:
  *"no agent images have been published yet"*, *"the agent image store cannot be reached…"*,
  and 502 for bytes that do not match the manifest. `create_app()` does no store I/O — the
  container still starts when the bucket is down.
- **Verification moved forward, it did not move away.** The old startup loader became
  `firmware/bundledir.py` and now **raises** instead of dropping: a publisher that skipped a
  corrupt bundle would report success and leave the flasher serving the previous build.
  Dropping-with-a-warning is still correct on the read side, where one bad manifest must not
  take the other three targets down.
- **The index read-modify-write race is knowingly accepted.** Two concurrent publishes of
  different targets can lose one entry. There is one publisher (an operator at a terminal),
  the loser is repaired by re-running one command, and a compare-and-set would need a
  generation precondition the seam deliberately does not expose. Revisit if publishing is
  ever automated in CI.
- **Prod is proposed, not applied.** `services/prod/docker-compose.yml` still sets
  `AGENT_IMAGES_DIR` and carries the now-false "no object store on purpose" comment; the
  replacement (`OBJECT_STORE_BACKEND: gcs` + bucket/prefix/impersonation) is written out in
  [docs/features/infrastructure.md](docs/features/infrastructure.md) → *Production hand-off*
  and root `CLAUDE.md` requires asking before touching production config. **Ordering is
  load-bearing: publish the bundles to GCS before deploying an image that no longer carries
  them**, or prod's flasher answers 503 in between.

Details: [docs/features/infrastructure.md](docs/features/infrastructure.md) → *Agent bundles
are served from the store*, [docs/runbooks/agent-build.md](docs/runbooks/agent-build.md),
[docs/runbooks/artifact-storage.md](docs/runbooks/artifact-storage.md).

## 2026-09-15 — the GCS credential is an impersonation, and signing is no longer local (S0-infra-5)

**Completes** the 2026-09-11 entry *agent bundles are artifacts, not image contents*, whose
accepted cost was "onboarding comes to depend on a store that today cannot be
credentialled", and the amendment to
`design/decisions/infrastructure-agent-bundles-are-artifacts.md`: *"the real prerequisite
is not an org-policy exemption: it is that `storage/factory.py` learns to accept a
credential that is not a key file."* It has. `gs://btvaroska` has now been round-tripped
end to end — put, get, a V4 signed URL redeemed with no credentials, delete — with **no
private key anywhere in the process**.

**Containment is the primary reason for it, not signing.** The old docstrings refused ADC
because ADC carries no private key. True, and the weaker argument. Prod's attached identity
is `mainsite@sites-470716`, the estate's shared VM account, and the bucket policy (read
2026-09-15, not inferred from a listing) grants it `roles/storage.objectAdmin` on the
**whole** of `gs://btvaroska`, unconditionally — including this estate's `secrets/` `.env`
backups. Plain ADC would make the `fleetforge-prefix-only` condition on
`fleetforge-artifacts` decorative. Impersonation would be the right answer even with an
org-policy exemption in hand, so **plain ADC is still refused, and neither credential set
is still an error** — never a fall back. Both credentials set is `ObjectStoreConfigError:
… mutually exclusive …`, the same "ambiguous configuration is refused, not resolved" rule
`factory.py` already applied to backends, now applied to identities.

**`signed_url` is a network call now, and there is exactly one code path.** Under
impersonation `generate_signed_url(version="v4")` POSTs to the IAM `signBlob` endpoint
through an `AuthorizedSession` with a backoff retry loop and no timeout of its own. The
adapter **cannot branch on this**: it is handed an opaque `bucket_factory` and has no idea
which credential is behind it. So signing always goes through `asyncio.to_thread` under
`_guard` — one wasted thread hop on a pure-CPU operation with a key file, the difference
between a timeout and a hung API without one. The same argument moved
`self._bucket_factory()` inside the guard in all four verbs: the first call resolves ADC
and mints a token, which on a non-GCP host hangs for seconds. `gcs.py`'s docstring said
*"Local CPU only — no `to_thread`, no I/O"*; that sentence became a lie the moment
impersonation was configurable, and it is gone.

**`ObjectStoreError`, never `ObjectStoreConfigError`, from the lazy path — the correction
that would otherwise be a 500.** `objectstore.py` states its contract: a config error is
raised "at construction/selection time, never mid-request-body", and
`api/deps.get_object_store` translates it only around `create_object_store`. So eager,
shape-only checks in `_gcs_store` raise `ObjectStoreConfigError`; anything discovered when
the credential is first *resolved* — no ADC, a refused token, a `signBlob` 403 — raises
`ObjectStoreError`, which every caller already maps to a retriable 503.
`test_gcs_missing_adc_fails_the_verb_as_a_backend_error_naming_adc` asserts the class
explicitly, because the two are siblings and neither `isinstance` check falls out of
`pytest.raises` alone.

**The failure that actually happens is a refused token, and untranslated it says nothing.**
An ADC that resolves but may not impersonate raises `RefreshError` **lazily, at first use**,
which `_guard` reports as `gcs get of … failed: RefreshError` — naming neither the
principal nor the missing role. `_impersonated_bucket` therefore refreshes eagerly and
translates, naming the target and `roles/iam.serviceAccountTokenCreator` **and nothing
else**: no token, no ADC path, and not the 403 body, which carries an opaque troubleshooter
id that is fine in a log and not in an exception that may reach a handler. Cost is zero —
the client would have minted that token on the very next call. Later refreshes still
surface generically; the first failure is the one an operator debugs.

**`project=None` is deliberate and must not be "cleaned up".** `storage.Client.__init__`
maps `None` to no project; the default `_marker` sentinel sends google-cloud-storage
looking for a project through ADC and raises when it cannot find one. The bucket is
cross-project and nothing here lists buckets.

**The principal is validated as a service-account email and never normalised.**
`<name>@<project>.iam.gserviceaccount.com`, lowercase. `boris@gmail.com`, a bare name, and
an uppercase spelling are all `ObjectStoreConfigError` — `resolve_key`'s and
`parse_blob_key`'s standing rule, applied to an identity. `.strip()` before the match is
the only repair allowed. The scope requested is `devstorage.read_write` only; signing needs
no scope at all, it is the *source* credential that needs `cloud-platform`.

**Measured, not assumed.** `just storage-check --backend gcs --blob` against real
`gs://btvaroska`: `SELFTEST OK`, `creds=impersonated(fleetforge-artifacts@…)`, and a URL
carrying `X-Goog-Credential=fleetforge-artifacts@…` that an unauthenticated GET redeems.
With no private key in the process, **that fetch is the `signBlob` verification**.
Containment was measured through our own adapter with `GCS_PREFIX=` empty — in-process
confinement deliberately off, so the IAM condition is what answers — and a `put` to
`secrets/ff-impersonation-probe-<uuid>.bin` failed `Forbidden`. Prod itself was **not**
exercised: `ssh prod` writes were refused by the sandbox classifier. Prod's identity holds
the same tokenCreator grant, so it is expected to pass, and S0-infra-6 owns running it.
Relevant prior art for the 403 that will eventually appear: root `docs/ops-log.md`
F-2026-08-18-001 (the `boris` podcast feed 500'd once on `signBlob` right after a deploy
and recovered by itself — IAM propagation; already RESOLVED, cited here as evidence only).

**Gotcha worth an hour to someone: on this dev box ADC is a USER, not `devserver@`.**
`gcloud config` shows the active account as `devserver@btvaroska`, but
`google.auth.default()` returns a `google.oauth2.credentials.Credentials` —
an `authorized_user` from `gcloud auth application-default login`, because the ADC **file**
wins over the metadata server. That principal has no tokenCreator binding and 403s on
`iam.serviceAccounts.getAccessToken`. `CLOUDSDK_CONFIG=/tmp/empty` for one command takes
the file out of the search path, the metadata server answers with this VM's attached
identity (`devserver@btvaroska`, which *is* a granted member), and the selftest passes.
This **corrects** the S0-infra-5 plan's measured claim that the dev box cannot impersonate
at all: it can, and that is what made the live verification above possible without prod.

**Not done, on purpose:** `services/prod/.env` and `services/prod/docker-compose.yml` are
untouched (root `CLAUDE.md` — never change production config without asking), so the
running container still has no object store and its comment *"the GCS service-account key
cannot be minted"* is stale. Wiring it is S0-infra-6's first act, and it is four env lines
with nothing mounted. `spec/` was not touched: `spec/device-protocol.md` already says
`artifact.url` is an opaque, short-lived signed URL, and how the server obtains a signature
is not wire-visible.

Details: `docs/runbooks/artifact-storage.md` (rewritten — the *BLOCKED* section is now
*resolved*, with the grant recipe, the dev-box gotcha and the signing-is-an-API-call
gotcha), `docs/features/infrastructure.md` → *A GCS credential that is not a key file*,
`docs/features/ota-deploy.md` → R1-BE-0 (landed).

---

## 2026-09-14 — the agent invalidates its own credential; the flasher erases nothing (S0-fw-4)

**Completes** the 2026-09-14 correction entry *"the brownout is ours, and the flasher erases
the calibration it was written to save"*, which diagnosed the defect and named this remedy.
**Supersedes the implementation half** of 2026-09-13 *"the flasher erases `nvs`, never the
whole chip"*: the reasoning in that entry stands unchanged — a chip-wide erase destroys the
cached RF calibration, and whether to clear credentials is a property of the write plan and
never a boolean on the write call — but the address it acted on was wrong. RF calibration is
in NVS, in IDF's `phy` namespace, not in the `phy_init` partition (which our build leaves
empty: `CONFIG_ESP_PHY_INIT_DATA_IN_PARTITION` is unset, the data is compiled into DROM). The
targeted wipe therefore destroyed exactly what it was written to save, on every flash, for
every board, forever.

After this task it is a property of neither. **The flasher erases nothing at all.**
`frontend/src/flash.ts::nvsWipe` is gone, the `wipeNvs` option and its checkbox are gone, and
`planWrite` now *asserts* that no part lands in `nvs` (`assertLeavesNvsAlone`, ranges rounded
out to the 4 KB sectors esptool actually erases, offset read from the table being written).
That guard is why `partitionTable.ts` survives with no wipe to aim: its purpose inverted from
"find `nvs`" to "prove we are not in it", and the new test that doctors a build so a part
lands at 0x9000 is the test that would have caught the original bug.

**Credential invalidation moved into the agent, because only the agent can act on one
namespace.** `ff_store_sync_token()` (`agent/main/ff_store.c`, called from `agent_main.c`
right after `ff_cfg_log`) erases `FF_STORE_NAMESPACE` — `ff`, and nothing else — when the
enrollment token in `ff_cfg` is not the one the stored credential was issued against. `phy`
survives. As a bonus it also covers boards re-flashed in the field with
`agent/tools/ff_cfg.py`, which a browser flasher never reaches.

**A digest, not the token.** The stored key is `tok_fp`: the first 8 bytes of
`sha256(cfg.token)` as 16 lowercase hex characters. The token is already in flash in `ff_cfg`
— it stays there after enrollment and nothing blanks it, which is the property the whole
design rests on — so storing it again is not a new exposure *in principle*, but **a digest is
loggable and a live single-use fleet-join credential is not**, and every diagnostic line in
this change wants to name the thing that changed. sha256 via `mbedtls` (already a REQUIRES,
already linked by esp-tls, so ~0 bytes) rather than a CRC, which would have saved nothing and
invited the question. 16 characters is inside NVS's string limits; `tok_fp` is inside its
15-character key limit.

**The four cases:**

| `cfg.token` | stored `tok_fp` | action |
|---|---|---|
| `""` / NULL | anything | nothing. A tokenless config — the QEMU smoke build, a diagnostic flash — must never cost a board its credential. Same refusal `ff_store_matches_api_base` already makes for a hostname change. |
| `T` | `== fp(T)` | nothing. The common case, every boot of a settled board. |
| `T` | `!= fp(T)`, or unreadable | `nvs_erase_all` → write `fp(T)` → one `nvs_commit`, in that order on one handle. Erase first because `nvs_erase_all` takes `tok_fp` with it; one commit makes the pair atomic, so a power cut leaves the board as it was and the next boot retries. A fingerprint we cannot compare is not proof the credential belongs to this token. |
| `T` | absent | **adopt: write `fp(T)`, erase nothing.** |

**Why absent ⇒ adopt and not erase — the decision a future reader will second-guess.**
"Absent fingerprint means this board predates the mechanism, so clear it to be safe" is a
landmine. At R2 an OTA replaces the agent *without* writing a new `ff_cfg`; the first post-OTA
boot of every board in the fleet would find no fingerprint, erase its credential, and try to
re-enrol with the long-spent token still sitting in `ff_cfg` → 409 → `park()`. That is a
fleet-wide brick delivered by an update. The migration cost of adopting instead is one extra
flash for the handful of boards enrolled before today, and the log says so in those words.
`nvs_open(NVS_READWRITE)` creating the namespace is likewise intended and safe: `ff_store_load`
probes `mqtt_pass`, not the namespace, so a namespace holding only `tok_fp` still reports
"nothing stored, enroll".

**One wholesale eraser remains, deliberately:** `agent_main.c::nvs_ready()`'s recovery path
still calls `nvs_flash_erase()` on an NVS that cannot be mounted (`NO_FREE_PAGES`,
`NEW_VERSION_FOUND`). That takes the calibration too, and it stays — there is no other way
back from an unmountable NVS — but its comment now says out loud that it is the last one.

**T2, without a bench.** QEMU has no radio and so never writes a `phy` namespace of its own;
seeding one with a canary before the first boot makes the calibration half provable anyway.
Three boots against `just up`: enrol → same token, `reusing the stored credential`, no HTTP
at all → new token written with the new `just agent-qemu-recfg` (writes `ff_cfg` into an
existing image at the manifest offset, NVS untouched; `--fresh` was the only previous option
and it is the opposite of this test), loud erase, `enroll 200`, both tokens `used` on the
server, device `online`. `nvs_tool.py` then still shows `phy/cal_data = ff-s0-fw-4-canary`
beside the *new* credential. Bench confirmation on real hardware is still owed, jointly with
S0-fw-3.

**Size:** `APP_SIZE_BUDGET_BYTES` ratcheted to the measured byte of a rebuild of all four
targets — esp32 991,776 → 993,696, esp32s3 971,168 → 973,136, esp32c3 1,026,240 → 1,028,336,
esp32c6 1,075,744 → 1,077,840 — for the new function and its three log strings. All four
bundles were rebuilt so `agent-check-fresh` stays green. Nothing on the wire changed, so
`spec/` was not edited —
enrolment is the same endpoint, the same payloads and the same single-use rule, and *when* a
device decides to re-enrol has always been device-local and unspecified.

## 2026-09-14 — the firmware catalog is keyed on (target, partition_layout) (S0-infra-7)

`firmware/catalog.py` now indexes bundles by `(target, partition_layout)` rather than
target alone, so two layouts for one chip can coexist. One layout exists today
(`ab-4m-v1`, frozen at R0); the moment a second appears, two bundles for one target
would otherwise collide on the same key and the flasher would have no way to ask for the
right one.

**A bundle directory is `<target>` or `<target>.<layout>`.** The dot-suffixed form is the
reader-side convention for two layouts; `just agent-build` still writes the bare
`<target>` form and is unchanged. `.` separates because no chip target and no layout id
contains one (both are `SAFE_SEGMENT`: lowercase alnum and `-`), so the split is
unambiguous — `esp32-ab-4m-v1` would not be. Directory/manifest mismatch (a directory
named `esp32.ab-8m-v1` containing a manifest with `partition_layout: ab-4m-v1`) is
dropped with a warning, the same rule as target/directory mismatch.

**The registry, not a relaxation.** Today `catalog.py` compares
`manifest.partition_layout` and `manifest.ota_slot_size` against two module constants.
The wrong fix is to drop the layout check so "two layouts both load" — that would let a
bundle declaring *any* string load, and `ota_slot_size` would float free of the layout
id, breaking the three-way contract `DECISIONS.md` 2026-09-09 protects. The right fix is
**`SUPPORTED_LAYOUTS: dict[str, int]`**, mapping every layout id the server understands
to the slot size a bundle claiming it must declare. A bundle cannot claim `ab-4m-v1` with
a 4 MB slot. It is a plain `dict`, not `MappingProxyType`/`frozenset` — tests register a
second layout with `monkeypatch.setitem(SUPPORTED_LAYOUTS, ...)`, which is the only way
to exercise multi-layout behaviour without a spec change.

**Absent layout resolves while unique; ambiguous requests name the layouts.** If `?layout=`
is omitted and exactly one candidate exists, `catalog.bundle(target)` returns it — the R0
case, and the backward-compatibility proof for every existing caller. If more than one
exists, it **raises `AmbiguousBundleError`** naming the layouts, which the download route
maps to 409. A `LookupError`, not an `AgentBundleError`: nothing is wrong with any bundle,
the request is under-specified, and the answer is to say so (`spec/standards.md`'s Unaided
onboarding rule) rather than to serve whichever sorted first and flash a board with the
wrong partition table. An unknown layout is a clean 404, same as an unknown target.

**S0-infra-6 hand-off:** the object-store key and the publish index must carry
`(target, partition_layout)` — `AgentBundle.key` is the shape to reuse. Do not re-narrow
it to target alone.

---

## 2026-09-14 — the blob key is store-relative, lowercase-only, and carries its own cache header

S0-infra-4 froze the content-addressed key scheme while **zero objects exist**: the same
change after R1 writes the first artifact is a migration over live bytes in a shared
bucket. **Extends, does not supersede, the *one storage model for every image* entry
below.** Code: `src/fleetforge/storage/blobs.py`, migration `0003_artifacts_and_builds`.

- **The key is store-relative, and this is the thing that would otherwise have been got
  wrong.** `design/artifacts.md` writes the layout as `fleetforge/blobs/sha256/<hex>`,
  which is the absolute *object* path. The `fleetforge/` half is the store's prefix,
  applied by `resolve_key`. So `blob_key()` returns `blobs/sha256/<hex>` and a
  `fleetforge/`-prefixed key is **refused**. Hardcoding the prefix would have written
  `fleetforge/fleetforge/blobs/…` in production and left dev (dedicated MinIO bucket, no
  prefix) and prod on two different layouts — invisible to every test anyone would think
  to write, visible only in a bucket listing months later.
  `test_blob_key_is_store_relative` is the guard.
- **Lowercase hex only, rejected and never repaired** — `objectstore.py`'s and
  `identity.py`'s standing rule, applied to the digest. `AB…` and `ab…` would be two
  objects holding one artifact. `parse_blob_key` accepts only `blobs/sha256/` + 64
  lowercase hex: nothing before it, nothing after it, no other algorithm.
  (Implementation note worth keeping: the Python regex anchors with `\Z`, not `$` —
  `$` also matches before a trailing newline, so `^[0-9a-f]{64}$` accepts `"<hex>\n"`.
  The PostgreSQL CHECK writes `$`, where POSIX has no such behaviour.)
- **`Cache-Control: public, max-age=31536000, immutable` is object metadata, not prose.**
  `ObjectStore.put` grew a `cache_control` parameter and both adapters send it *only*
  when it is not None, so an ordinary `put` is byte-for-byte the request it always was.
  A header asserted only against a fake bucket is a header nobody has seen on an object,
  so `just storage-check --blob` reads it back off a signed-URL GET and
  `tests/test_object_store_minio.py` does the same against real MinIO.
- **`builds.outputs` is JSONB with no foreign key, and that has a price.** A bundle build
  produces four parts, so one `artifact_sha256` column cannot hold the result and a
  per-part row would collide on the cache-key PK; the set is consumed as a unit. But
  PostgreSQL cannot FK into JSONB, so **a future pruner (R2) must treat `builds.outputs`
  as a GC root** rather than trusting referential integrity to keep a referenced blob
  alive. A `build_outputs` join table is the additive migration the day part-wise
  queries appear. Likewise there is deliberately no `artifacts.storage_key` (the key is
  a pure function of the PK; a stored copy is a second spelling that can disagree) and
  no refcount (nothing decrements it yet, and a refcount with no decrementer is a lie).
- **Both tables land empty with no readers**, the same posture `fleetforge.storage` took
  at R0-be-6. That is what makes `downgrade()` an honest reverse here, and it will not
  be true next time.
- **The wire-visible half was proposed, not edited.** `spec/device-protocol.md` already
  hands a device `artifact: {url, sha256, …}` and already says `url` is a short-lived
  signed URL, so the key scheme is not wire-visible and no spec change is required. The
  optional clarification carried to review, unedited: *`artifact.sha256` is the
  artifact's identity — the server stores the bytes under that digest and nothing else.
  `artifact.url` is **opaque**; a device must never construct, cache-key on, or parse
  it.*

---

## 2026-09-14 — `build_digest` covers the inputs, not the clock

S0-infra-3 adds `config_sha256` and `build_digest` to every agent bundle manifest. The
part worth recording is what goes *into* `build_digest`, because it is the thing a future
reader will second-guess.

**In:** a version tag (`v: 1`), `target`, `agent_version`, `idf_version`, `idf_image`,
`source_commit`, `partition_layout`, `ota_slot_size`, `config_sha256`, and each part's
`name`/`offset`/`size`/`sha256` sorted by name. Canonical JSON
(`sort_keys=True, separators=(",", ":")`), then sha256.

**Out, and this is the decision: `built_at`.** A build id has to answer *"is the bundle
on my bench the one you built?"*. A timestamp inside it makes every rebuild of identical
inputs look like a different build, and the field becomes decoration. Identical inputs →
identical digest is therefore a property, not an accident, and
`tests/test_agent_manifest_identity.py` asserts it directly — nothing else would catch a
regression there. `flash_size` and `chip_family` are out too: both are functions of target
and config, already covered.

Two smaller calls made with it:

- **One definition, verified rather than duplicated.** `verify_bundle.py` imports
  `build_identity` from `make_manifest.py` (same directory, `sys.path[0]` resolves it,
  both stdlib-only so they still run inside the ESP-IDF builder image). A second copy of
  the canonical serialisation would drift and the drift would present as a false mismatch
  on a good bundle.
- **Absent identity warns; malformed identity drops.** A bundle with no `config_sha256` is
  old, not invalid — dropping it would take the flasher offline for a cosmetic reason. A
  bundle with a digest that does not match `^[0-9a-f]{64}$` is dropped, because a corrupt
  digest is one that gets compared and believed. `MANIFEST_SCHEMA` stays 1: both fields are
  additive and optional, so a reader has nothing to switch on. It bumps when one is made
  required, which S0-infra-6 may want once every bundle comes from the object store.

Gotcha for the next person who needs an A/B firmware build: pick a config lever that is
not already set. The planned `CONFIG_ESP_MAIN_TASK_STACK_SIZE=4096` was a no-op waiting to
happen — the repo already sets it to 8192 — so the verification moved that existing line
instead. An option set to its current value leaves `sdkconfig.resolved` byte-identical and
proves nothing.

---

## 2026-09-14 — one storage model for every image: content-addressed blobs, manifests as views, builds as a cache

Asked whether growing image count means moving from fixed artifacts to dynamic build
with caching. It does not: **prebuilt-and-cached is the steady state and a build is what
happens on a cache miss.** Onboarding must never wait on a compile — there is always a
pinned known-good bundle set that needs no builder running. Design in
[design/artifacts.md](design/artifacts.md); tasks S0-infra-3 … S0-infra-7.

**Extends, does not supersede, `design/decisions/infrastructure-agent-bundles-are-artifacts.md`**
(2026-09-11). That ADR decided agent bundles move behind `ObjectStore` and rejected a
baked fallback tier; both hold. This entry says what the storage underneath looks like
once they get there, and finally files the tasks — the ADR has sat "Accepted, not yet
implemented" for three days with nothing in `TODO.md` pointing at it.

- **Artifacts are content-addressed**: `fleetforge/blobs/sha256/<hex>`, write-once,
  `Cache-Control: immutable`. `objectstore.py::put` already documents overwrite-is-safe
  *because* R1 content-addresses; this makes the key scheme real before R1 writes the
  first object. Dedup is a side effect that pays for itself immediately — a new agent
  version changes `app.bin` and nothing else.
- **Manifests are generated views, not the storage unit.** A manifest for a given
  (target, layout, version) becomes a query rather than a directory of copied bytes,
  which is the only thing that makes the coming combinatorics tractable: 4+ targets ×
  layouts × retained versions, then V2's repo × ref, then V3's delta images, which are
  indexed by *pairs* of versions and therefore quadratic.
- **Per-device data stays out of artifact identity.** `ff_cfg` as a separate part is
  what lets N devices share one artifact, and it is the structural advantage over
  ESPHome's compile-per-device. Adopted as a standing constraint on future features.
- **The R9 build-cache key is wrong as specified.** `docs/features/build-pipeline.md`
  says `(repo, ref, toolchain)`; that omits build configuration, so two builds of the
  same ref with different `sdkconfig` collide. Corrected to
  `H(idf_image_digest, target, partition_layout, source_tree_digest, config_digest)`.
  Source *tree*, not commit — a dirty tree must not hit a stale entry.

**What this came out of, and it is the same lesson as the entry below.** The manifest
records `agent_version`, `source_commit`, `idf_version` and a digest-pinned `idf_image`,
and still could not answer "which build produced this failing bundle?" — because it
carries no digest of the **build configuration**. Establishing that every brownout on
record came from a 160 MHz `-Og` build took three sessions and a `git log` correlation
against a timestamp. Provenance that names the inputs but not the configuration is
provenance that cannot settle an argument. `config_sha256` in the manifest and in the
`S0-fe-7` diagnostic bundle (S0-infra-3) is a few lines and would have made it a string
comparison.

Also noted, not yet fixed: `agent_version` (`0.2.0`) and the server release version
(`v0.3.3`) are two schemes sharing one word in the prose. The wire protocol already
keeps `fw_version` and `agent_version` apart; the docs should follow it.

---

## 2026-09-14 — the brownout is ours, and the flasher erases the calibration it was written to save

Two corrections, both from one observation. **Supersedes the 2026-09-13 entries
*"reducing TX power does not break the brownout loop"* and *"the flasher erases `nvs`,
never the whole chip"*, and the hardware conclusion in S0-fw-3.**

**The observation.** A *brand-new* ESP32 board — same cable, same port that fail under our
image — was flashed with ESPHome, associated to Wi-Fi and ran. A new board has no cached
RF calibration, so ESPHome performed the same cold full calibration our image dies in, on
the same rail, and survived it.

- **The supply carries a cold full calibration.** The 2026-09-13 entry named the
  discriminating experiment ("flash the stock Arduino sketch with a full chip erase … if
  it survives, our image draws more than it needs to") and called "the supply is marginal"
  the best-supported reading. That reading is now falsified, and so is the
  bulk-capacitance-across-3V3/GND conclusion it pointed at. **The fault is in our image or
  our build configuration.**
- **Every brownout on record was produced by a build we no longer ship.** The
  2026-09-13T14:15 bundle is agent `19b0a0b`, which predates `d705652` (`-Os`, 80 MHz, max
  modem sleep, TX-power ladder). So all six failing boots ran at **160 MHz with `-Og`**.
  The entry below concedes 80 MHz was "untested on hardware"; what was not noticed is that
  it is untested *against the only failure we have*. Retesting costs one flash.
- **A lead the sdkconfig already contains.** We build with `CONFIG_ESP32_REV_MIN_0=y`
  (IDF's default). In IDF v5.5 `components/esp_hw_support/port/esp32/Kconfig.hw_support`
  the rev-0 option carries `select ESP_BROWNOUT_USE_INTR`, justified inline as *"Brownout
  on Rev 0 is bugged, must use interrupt"* — so our min-revision choice force-enables the
  interrupt-based detector. If the board is rev 1 or 3 and ESPHome builds for a higher
  min revision, the two images use **different brownout mechanisms on the same silicon**,
  which reproduces this symptom with no difference in current draw at all. Unresolved;
  the revision is in the boot banner of bundles already collected.

**The second correction: the flasher preserves a partition that holds nothing.** The
2026-09-13 entry below replaced `eraseAll` with a targeted `nvs` wipe in order to keep
"the `phy_init` partition holding the cached RF calibration". RF calibration is not in
`phy_init`; it is in **NVS**, under IDF's `phy` namespace — exactly the bytes `nvsWipe`
fills with 0xFF. With `CONFIG_ESP_PHY_INIT_DATA_IN_PARTITION` unset (our build) the
`phy_init` partition at `0x11000` is unused entirely, the init data being compiled into
DROM. The error code in every bundle says so: `0x1102` is `ESP_ERR_NVS_NOT_FOUND`.

So the reasoning in that entry survives intact and the implementation does not: erasing the
calibration on every flash *is* the mechanism it identified, and the fix aimed at the wrong
address. **No fleetforge-flashed board can currently retain a calibration.** Filed as
S0-fw-4; the remedy is to stop wiping from the flasher and let the agent erase
`FF_STORE_NAMESPACE` when the `ff_cfg` token fingerprint changes, which is the only place
with namespace granularity. Note this does **not** explain the brand-new board above —
nothing was cached either way — so the two defects are independent and both are open.

**Method note, worth more than either finding.** Both errors have the same shape: a
comparison against another toolchain ("a stock sketch runs fine") read as evidence about
*hardware*, when the toolchains also differed in what they erase and how they are built.
The comparison is only informative when the other side's configuration is known. The
mechanical version — diff `agent/dist/<target>/sdkconfig.resolved` against the other
build's `sdkconfig` — was available the whole time and was never run. Do that before
theorising about current draw again. Wider ESPHome review, including what is worth reusing:
`products/docs/esphome-review.md`.

---

## 2026-09-13 — the agent's power and size posture: 80 MHz, `-Os`, max modem sleep, a TX-power retry ladder

Came out of a review prompted by the observation that the agent image looked large for
what it does. The source was not the problem — ~2,070 lines of C excluding comments, all
of it load-bearing. The build configuration was. Four changes, two of which are also
candidate levers for S0-fw-3.

- **`-Os` instead of IDF's default `-Og`.** The connect-only agent was 1,079,520 bytes of
  a 1,966,080-byte OTA slot — 55% full before R2 adds OTA and R5 adds signature
  verification. The optimization level alone was worth 8-9% on every target (esp32
  1,079,520 → 990,544; s3 1,060,800 → 969,824; c3 1,129,856 → 1,024,848; c6 1,181,840 →
  1,074,368). Assertions stay on: `-Os` is independent of them, and the agent's
  diagnostic output is the product. The cost is less faithful panic backtraces, which is
  a real cost to the serial console's classifier and was taken knowingly.
- **80 MHz CPU, down from 160.** This agent is I/O-bound by construction — DHCP, two TLS
  handshakes, one small JSON every `hb_s`. 160 MHz bought nothing measurable and cost
  ~20-30 mA continuously. **Also a live S0-fw-3 candidate**, which is why it is in
  `sdkconfig.defaults` next to `REDUCE_TX_POWER` and not in a performance note:
  `esp_clk_init()` applies it before `app_main`, so it is in effect during PHY
  calibration, and the CPU is running flat out alongside the calibration at 160 MHz. The
  v0.3.3 result ruled out TX power as the dominant draw; it did not rule this out.
  Untested on hardware as of this entry.
- **`WIFI_PS_MAX_MODEM`, stated rather than inherited.** IDF's default is
  `WIFI_PS_MIN_MODEM`, so the radio already slept — but as an accident of the SDK's
  default that an IDF pin bump could change, in a file whose whole style is to say why.
  `MAX` rather than `MIN` because the workload already chooses to be deaf for tens of
  seconds (30 s MQTT keepalive, `hb_s` heartbeat). The cost is seconds of `dn/cmd`
  downlink latency, accepted: every command this product sends is part of a deploy, and
  no human waits on one interactively. Revisit if R2 grows an interactive command.
- **The Wi-Fi reconnect walks a TX-power ladder instead of repeating one attempt.**
  Retrying forever is only useful if the attempts differ; an identical attempt repeated
  for a year is a stuck board that looks busy. After every 3 consecutive failures the
  radio steps down (default → 14 dBm → 8 dBm) and wraps. The counter-intuitive part is
  that *lowering* power can make association succeed: the auth/assoc frames at full power
  are the biggest current transient in the sequence, and on a marginal rail that is what
  drops it under the brownout threshold mid-association.
  - **Runtime `esp_wifi_set_max_tx_power`, not compile-time
    `CONFIG_ESP_PHY_MAX_WIFI_TX_POWER`.** This is the per-board version of the knob
    S0-fw-3 deliberately refused to turn fleet-wide. It costs range only on a board that
    has already proven it cannot associate at full power, and only while that is true.
  - **Rung 0 is read from the driver, not hardcoded to 20 dBm.** On a post-brownout boot
    `CONFIG_ESP_PHY_REDUCE_TX_POWER` has already brought the PHY up at minimum power, and
    a ladder that "restored" a literal 20 dBm would silently undo S0-fw-3 on exactly the
    board it was written for.
  - **A working rung is kept, not reset.** A board that could only associate at 8 dBm
    will not survive its first data frame at 20 — the rail that failed during association
    was not repaired by it succeeding. Wrap-around still reaches full power again, which
    is what lets a board that was moved, or whose supply was fixed, climb back with no
    re-flash.

**The retry count is deliberately still unbounded** — considered and rejected in the same
review. A board that stops trying to reach its network is a site visit, which is the
intervention this product exists to remove, and an AP reboot or a day-long uplink outage
is survivable only by a board still trying when it ends. The fix for "stuck in a loop" is
to vary the attempt, not to stop making it.

Guarded by `tests/test_agent_power_and_size.py`, deliberately separate from
`test_agent_partitions.py`: everything here is OTA-recoverable, and that file's value
comes from every line in it being a physical-recall mistake. The size budget is per
target, ratcheted down after each measured win.

---

## 2026-09-13 — the flasher erases `nvs`, never the whole chip

Every flash used to pass `eraseAll: true` to esptool-js, which erases the entire chip —
including the `phy_init` partition holding the cached RF calibration. That was wrong, and
it is the one thing on our side that was demonstrably making the brownout loop
unescapable: the calibration is written only after a boot survives the full calibration,
the largest current draw in startup, so erasing it on every flash guarantees the expensive
path on every freshly flashed board, forever. A board with a marginal rail can never
bootstrap out, because the thing that would save it is deleted on each attempt.

- **It also explains the comparison that kept confusing us.** Arduino's uploader writes
  bootloader, partition table and app and leaves `nvs` and `phy_init` alone. So "the same
  cable and port run a stock sketch fine" was never evidence that the supply is adequate —
  the sketch inherits a calibration it never has to re-earn. Ours re-earns it every time.
- **`nvs` still has to go.** A board that already enrolled keeps its broker credential
  there and reuses it (R0-fw-1 logs "reusing the stored credential"), so the freshly minted
  token in `ff_cfg` would never be spent. Implemented as an explicit part in the write plan
  — 0xFF over exactly that partition — because `writeFlash` erases the sectors it writes
  and NVS reads an erased sector as empty. esptool-js 0.6.1 has no `eraseRegion`, or this
  would be one call.
- **The offset is read, not hardcoded.** `partitionTable.ts` parses the table being written
  to the board in the same operation and looks `nvs` up by label. `0x9000` is right for
  `ab-4m-v1` and need not be for the next layout, and a wipe aimed at a stale constant
  erases the wrong 24 KB of a real board.
- **`eraseAll` is gone from `WriteOptions` entirely**, not defaulted to false. Whether to
  clear credentials is a property of the write PLAN; leaving the flag on the write CALL
  would put a whole-chip erase one boolean away from returning.
- **This does not fix the board that prompted it.** That board has never completed a
  calibration, so there was nothing to preserve. What changes is that once any board gets
  through once — on a better supply, or with bulk capacitance — a reflash no longer throws
  it away. See the entry below for what is still unresolved.

---

## 2026-09-13 — reducing TX power does not break the brownout loop (supersedes 2026-09-12, S0-fw-3)

The entry below claims `CONFIG_ESP_PHY_REDUCE_TX_POWER=y` ends the loop. On the one board
that has ever been in the loop, it does not. Recording that here because the claim shipped
in v0.3.3 and an untested claim left standing is how the next person wastes an evening.

- **The evidence is an A/B inside one log.** Diagnostic bundle 2026-09-13T14:15, device
  `8c94df4cf3f8`, agent `19b0a0b`. Its first boot follows a non-brownout reset, so
  `esp_reset_reason() != ESP_RST_BROWNOUT` and the PHY came up at full power; boots two
  through six each print `the previous boot ended in a BROWNOUT`, so the reduction was
  active. All six die identically at `phy_init: failed to load RF calibration data
  (0x1102), falling back to full calibration` → `E BOD: Brownout detector was triggered`.
  The 774 ms / 822 ms difference between them is the UART time to print that warning line,
  not progress.
- **The lever was aimed correctly; it just has no effect here.** IDF v5.5.5
  `components/esp_phy/src/phy_init.c` calls `esp_phy_reduce_tx_power(init_data)` before
  `register_chipv7_phy(init_data, cal_data, calibration_mode)`, and an empty NVS forces
  `calibration_mode = PHY_RF_CAL_FULL` regardless of `CONFIG_ESP_PHY_CALIBRATION_MODE` — so
  the lowered power table is in force *during* the full calibration. The conclusion is not
  that the option was misapplied but that TX power is not what dominates a cold
  calibration's current draw on this hardware.
- **The change stays in.** It is gated on the brownout reset reason, inert on a healthy
  board, and costs nothing. Reverting it would buy nothing either. What changes is the
  claim attached to it: it is a plausible mitigation with one negative result, not a fix.
- **The reporting half of S0-fw-3 is unaffected and does work.**
  `agent_main.c::log_power_fault()` and the `brownout` progress stage behave exactly as
  designed — the warning line appears on every post-brownout boot in the bundle above.
  That half is what made this negative result legible at all.
- **What would actually settle it is a hardware experiment, not a firmware one.** Flash the
  stock Arduino Wi-Fi sketch with a full chip erase onto the same board, cable and port. A
  brownout there rules out every firmware avenue and points at bulk capacitance across
  3V3/GND; survival there means our startup draws more than it needs to. Until that runs,
  "the supply is marginal" is the best-supported reading but is not proven against our own
  image.

---

## 2026-09-12 — brownout recovery is targeted, not a fleet-wide power cut (S0-fw-3)

A board with no cached RF calibration browns out inside `phy_init`'s full calibration and
cannot escape: the calibration is only written back once a boot survives it, so every boot
is identical. Reported by an operator whose cable and port demonstrably flash and run a
plain Wi-Fi sketch — which they do because that sketch inherits a calibration it never has
to re-earn. The fix is **`CONFIG_ESP_PHY_REDUCE_TX_POWER=y`**: after a brownout reset the
PHY comes up at its lowest TX power, often enough to get through once, and one survived
boot ends the loop for good.

- **Fleet-wide TX power stays at 20 dBm.** `CONFIG_ESP_PHY_MAX_WIFI_TX_POWER` was the
  obvious alternative and is the wrong trade: it costs range on every board in the fleet,
  permanently, to fix a fault some boards have on their first boot only. The IDF option
  above is the same idea aimed at the boards that need it — it is gated on
  `esp_reset_reason() == ESP_RST_BROWNOUT` and is inert on a healthy board.
- **It lives in `agent/sdkconfig.defaults` but is NOT a flash-time immutable.** Everything
  else in that file is (partition table, bootloader rollback, the compiled-in CA bundle);
  this one ships in the app image and an OTA can add or remove it. It is there because
  that file is the one place the agent's posture is read from, and the comment says so.
  Deliberately **not** added to `REQUIRED_SDKCONFIG` in `tests/test_agent_partitions.py`:
  that list's criterion is "no OTA-free fix exists", and an OTA-fixable entry would blur
  what the guard means.
- **The escape is reported, not just achieved.** `agent_main.c::log_power_fault()` reads
  `esp_reset_reason()` and states the previous boot's brownout outright, and the agent
  sends a `brownout` progress stage just after `link_up`. Both exist because the recovery
  is otherwise invisible: the board reboots, and the BOD line and the reset banner belong
  to the boot that died. A board that browns out and then recovers looked flawless on the
  dashboard and in the flashing console. `brownout` sits outside the stage walk — it is
  retrospective — which is why it is reported after `link_up` and not before.
- **Why the banner cannot be trusted here.** `CONFIG_ESP_BROWNOUT_USE_INTR=y` means the
  BOD ISR restarts the chip, so the next boot prints `rst:0x3 (SW_RESET)` rather than
  `RTCWDT_BROWN_OUT_RESET`. The ISR does set the reset-reason hint, so `esp_reset_reason()`
  is right where the banner is misleading.

Details: `agent/sdkconfig.defaults` (the comment block), `TODO.md` → S0-fw-3.

## 2026-09-11 — a bundle is stale when its provenance predates the agent sources (S0-infra-2)

The staleness rule is **git ancestry over the agent source pathspec, not mtime**. A bundle
whose `manifest.json:source_commit` predates the newest `git log -1 -- agent :(exclude)agent/dist`
is stale and fails `just agent-check-fresh`, which gates `just build` (the app-image
pipeline). Four verdicts: **fresh** (built from a commit containing the newest agent
change), **STALE** (ancestor check fails), **UNTRACEABLE** (`source_commit` absent/unknown
or not a commit in this repo), **NOT BUILT** (no manifest). A fifth, **DIRTY SOURCES**,
refuses the release path when `git status --porcelain -- agent :(exclude)agent/dist` reports
uncommitted changes — a bundle records HEAD, not what was compiled.

- **Why git ancestry, not mtime.** The TODO's literal wording was "bundle is older than
  `agent/main/`" by mtime. `git checkout`, `git pull` and branch switches rewrite source
  mtimes with no content change; a clone sets them all to clone time. A check that fires
  on a correct tree is the check people delete — the runbook already carries that lesson
  verbatim about a `CONFIG_SECURE_BOOT_V1_SUPPORTED` prefix match. Git ancestry answers
  the same question ("does this bundle contain the newest agent source change?") and
  cannot be wrong about it. This deviation from the TODO's wording is deliberate.
- **The dirty-tree refusal lives in the release path only**, never in `just agent-build`.
  Dirty-tree builds are the firmware dev loop (edit → build → QEMU → commit), and
  breaking that would get the guard deleted. The release path gets condition 4 because
  `make_manifest.py` records `git rev-parse HEAD`, so a bundle built from a dirty tree
  claims provenance it does not have — the runbook already says "commit before building
  anything you intend to push", and refusing a dirty agent tree in `just build` closes
  the one place that rule is otherwise unenforced.
- **Source pathspec is `agent/` minus `agent/dist/`**, i.e. exactly the Docker build
  context `agent/.dockerignore` defines (`COPY . /project`). Rejected narrower variants
  (per-target `sdkconfig.defaults.<target>`, "only `agent/main/`"): they drift from
  `.dockerignore`, and the whole point is that anything that can change a bundle is
  counted. Over-strict costs a rebuild; under-strict costs a fleet.
- **No bypass env var.** v0.3.0 shipped stale knowingly; the harm was that it became
  invisible afterwards. If a stale ship is wanted again, `just agent-build-all` is 20
  minutes, and deleting a justfile line is a reviewable commit.
- **The esp32 bundle was stale too**, which the filed task did not know. All four targets
  were rebuilt. `agent/dist/esp32/manifest.json` recorded `source_commit 81aea08`
  (S0-fe-7), while the newest commit touching agent sources was `43aeb31` (S0-fw-2,
  "hold pre-clock stage reports"). So the shipped esp32 bundle was missing the S0-fw-2
  fix — consistent with DECISIONS.md 2026-09-11 S0-fw-2: *"Real boards do not have this
  fix yet, and no commit here can give it to them."*

Details: `docs/runbooks/agent-build.md` → *Staleness*, `agent/tools/check_bundles_fresh.py`
(the guard), `tests/test_agent_bundle_freshness.py` (8 cases).

## 2026-09-11 — a stage report that predates the clock is held, not lost (S0-fw-2)

`link_up` — the report the entry below calls "a board is visible as `arriving` before it
exists in the fleet at all" — had never once reached a real server. It is sent the moment
the link comes up, which is before `ff_time_sync()`, and `CONFIG_MBEDTLS_HAVE_TIME_DATE=y`
(R0-fw-1, deliberately) makes a TLS handshake at epoch 0 fail certificate validity. The
POST never opened, the failure logs at DEBUG by design, reports are never retried, and the
stage was gone. Reproduced against prod with the pre-fix bundle before anything was edited.

- **The gate is the URL scheme AND the clock, and the scheme half is not defensive
  padding.** `transport_ready() := !tls || ff_time_is_sane()`. A clock-only gate is the
  obvious reading of the bug and it would have **regressed the one configuration where
  the feature already worked**: the plaintext lab runs `http://` with `--no-ntp`, the
  clock never becomes sane, and `link_up` would have been held forever. `tls` is decided
  once in `ff_progress_init()` from the scheme of the built URL, which is the same
  property `ff_cfg.h` already documents as selecting TLS. Both directions were run.
- **Buffer-and-drain, not "move the call below the sync".** The two-line version makes
  `link_up` a lie about when the link came up, delays the first sign of life by up to
  `SNTP_TIMEOUT_MS` (15 s), and fixes exactly one call site — the next pre-clock reporter
  re-introduces the bug. `agent_main.c` is comments-only for that reason.
- **Drain BEFORE the current report's POST.** The server timestamps at receipt and
  `progress.py` breaks `at` ties with `id`, so a held `link_up` inserted immediately
  before `time_synced` still sorts ahead of it. Drain-after would invert the pair in the
  same clock tick. Measured on prod: `link_up` 00:42:24, `time_synced` 00:42:27.
- **No `ff_progress_flush()`.** `agent_main.c` already reports `time_synced`
  unconditionally right after the sync, so the drain point exists for free and cannot be
  forgotten; a public flush would be an ordering trap for whoever omits it.
- **One attempt per held stage, and the first failed send abandons the rest.** Property 2
  (never retried) applies to a held report as much as a live one, and abandoning bounds
  the added boot-path cost at one `PROGRESS_TIMEOUT_MS` (5 s) instead of depth × 5 s. A
  failed open means no route; the remaining stale stages are not worth 15 s of the enroll
  path.
- **Re-check `armed` after the drain.** A held entry can take the 401 branch, and
  continuing to POST the current stage afterwards is exactly the "keep talking with a
  credential the server called dead" behaviour property 3 exists to prevent. This is the
  one ordering hazard the change introduces and it is invisible in a plaintext lab. A 401
  clears the queue as well: those stages can never do anything but sit in RAM.
- **`FF_PROGRESS_MAX_STAGE` is 32, not 24.** The server's stage regex is
  `^[a-z][a-z0-9_]{0,31}$`, so 32 characters is the widest legal stage a future caller
  could pass, and a silent `strlcpy` truncation would turn a valid stage into a
  *different* one. Retyped across the seam with the same comment `PROGRESS_MAX_DETAIL`
  carries. A held entry stores `stage` + `detail` only — never the token, which stays in
  `s_state` and is inserted at build time, so the existing wipe-the-body discipline still
  covers every copy of it.
- **PROPOSED `spec/device-protocol.md` wording, deliberately not written** (same
  treatment S0-fw-1 gave `POST /v1/device-progress`). Under *Clock — SNTP before TLS*:
  "Stage reports (`POST /v1/device-progress`) produced before the clock is set are held by
  the agent and sent, in order, on the first report after the sync. Their `at` is the
  server's receipt time, so a held stage reads as slightly late; the ordering is exact."
- **Real boards do not have this fix yet, and no commit here can give it to them.**
  `agent/dist/` is gitignored and the flasher serves the bundle baked in by `COPY
  agent/dist /app/agent` (R0-infra-2), so prod hands out the buggy firmware until the next
  release build. Precisely the coupling *agent bundles are artifacts* (2026-09-11) exists
  to remove; out of scope for a 0.25 d firmware fix.
- **Worth keeping: `-Werror` is the only static gate firmware gets.** There is no host-side
  C test harness in this repo, and `agent-qemu-smoke` runs a **tokenless** config, so the
  reporter is unarmed and the smoke check cannot exercise the queue at all. Green smoke
  means "still boots". The behaviour is proved against prod or not at all.

Details: `docs/features/enrollment.md` → *The first stage survives the clock (S0-fw-2)*,
`docs/runbooks/agent-qemu.md` → *Proving the clock rule* (third direction).

## 2026-09-11 — the escalation path is one click, and redaction is not the firmware's job (S0-fe-7)

The fourth layer of *Unaided onboarding*. On 2026-09-11 the diagnosis was already on
screen and still had to be re-typed into a chat window, because selecting text in an
unlabelled `<pre>` is not an affordance anyone finds. **Copy diagnostic bundle** now
assembles the header, the fault, the boot progress, the chip, the config and the whole
console log into one string, puts it on the clipboard and renders it in a `readOnly`
`<textarea>`.

- **Redaction happens at one choke point, not per field.** `redactSecrets` is applied
  once to the fully assembled string, as the last statement of `buildDiagnosticBundle` —
  a comment marks it as the only `return` that function may have. Per-field redaction
  fails open: the next section someone adds is unredacted by default, and the section
  most likely to carry a live credential is the raw board log, which nobody remembers to
  filter. A bundle is *designed* to be pasted into a chat window, so it is the single
  most likely way a live secret leaves the machine.
- **Three rules, because a secret arrives three ways.** (1) A literal scrub of what the
  page was handed (`split`/`join`, never `new RegExp(secret)` — a passphrase is arbitrary
  text and `.*` would eat the bundle), with a `MIN_SCRUB_LENGTH = 4` floor so a
  two-character "secret" cannot shred the log. (2) URI userinfo
  `scheme://user:pw@host` → `user:[REDACTED]@`, which reaches a broker credential this
  page was never given because the *firmware* printed it; the username survives, it is
  diagnostic and not a credential. (3) Token shapes `ff[ae]_…` with a `{6,}` length
  floor, so the panel's own prose ("re-flash with a fresh `ffe_` token") stays readable.
  Each rule has a test that fails when only that rule is removed — checked by deleting
  each in turn.
- **Two agent versions are printed, not one.** What this page would flash (the manifest)
  and what the board says it is running (the `ff-agent` banner). They differ exactly when
  the board is carrying a stale flash, which is invisible from either number alone.
- **`window.location.origin`, never `href`.** A path or query string can carry a token;
  the origin is the only part that answers "which deployment is this?".
- **The `/v1/healthz` fetch for the server version is silent on failure.** It never sets
  `manifestError` and never triggers `onSessionExpired`: a bundle that cannot name the
  server version is still worth pasting, and an unreachable server must not colour the
  flasher page red.
- **The textarea shows a snapshot taken at the click, not a re-derivation.** The summary
  ticks at 1 Hz while watching, so a derived box would drift from what the clipboard got
  within a second. It is set *before* the clipboard write, so a browser that refuses the
  clipboard still leaves the full bundle on screen with a sentence saying so.
- **Deviation from the plan.** The plan's format example echoed the offending log line in
  the fault section; that made `E BOD: Brownout detector was triggered` appear four times
  while the same plan's acceptance requires exactly three (once per cycle, so "how many
  times did this board brown out?" is answerable by eye). The fault section therefore
  prints the plain-English hint plus `named by  line N of the console log below` and
  never reprints the line. The diagnosis still lands in the first fifteen lines.

## 2026-09-11 — agent bundles are artifacts, not image contents (planned, infrastructure)

The application image will ship **zero** agent firmware bundles; they move behind
`ObjectStore` onto the same distribution path as R1's user artifacts, so publishing a
bundle stops requiring an app rebuild and deploy. Reverses the distribution half of
R0-infra-2's `COPY agent/dist /app/agent` (its build half is unchanged) on two grounds:
the target list only grows (~1.2 MB per chip, with H2 / Thread / RPi already on the
roadmap), and the coupling has already produced its defect once — `S0-infra-2`, three
stale bundles shipped in v0.3.0. A baked fallback tier was considered and rejected as
preserving the defect with an extra branch. Accepted cost, stated: onboarding comes to
depend on a store that today cannot be credentialled, making the GCS blocker in
`docs/runbooks/artifact-storage.md` a hard prerequisite rather than a caveat. Details:
[design/decisions/infrastructure-agent-bundles-are-artifacts.md](design/decisions/infrastructure-agent-bundles-are-artifacts.md),
[docs/features/infrastructure.md](docs/features/infrastructure.md) → *Agent bundles served from the object store*.

## 2026-09-11 — the panel performs the fix it names (S0-fe-6)

Layer 3 of *Unaided onboarding*. The panel already named the fault (S0-fe-4) and always saw
the boot (S0-fe-5); it still answered a spent token with three manual steps written as prose.

- **There are exactly two mechanisms, because the panel's only channel to the board is the
  serial port:** `reboot` (pulse EN through the console session) and `reflash` (release the
  port, re-acquire with esptool, mint, write, erase). "Retry enrol" and "mint a fresh token"
  from the task text are **not** separate actions and collapse into `reflash` — the agent
  exposes no serial command surface, and a token that is not written into `ff_cfg` changes
  nothing. Anyone who later wants a third remedy needs a new channel first.
- **The rule that decides who gets a button: the board will not fix itself AND the action
  changes the outcome.** `agent_main.c` is what fills the table in, not taste.
  `enroll_until_credentialed()` parks forever on 401/409, so only a re-flash moves that
  board. Everything else retries by itself — 60 s → 15 min for a 503, `while
  (ff_net_bring_up(...) != ESP_OK)` forever for the link, DHCP forever — and **the agent's
  own retry ladders are precisely what disqualify 503, DHCP and link from having a button**.
  A button that restarts a retry already in progress is the thing the task forbids.
  Brownout and wrong PSK render no button because no software fixes a cable or a PSK.
- **The recovery re-flash always erases, and it is deliberately NOT the form's checkbox.**
  Every fault a re-flash fixes is a spent token or a stale NVS credential, and
  `ff_store_load()` short-circuits enrolment while a credential is present — so a freshly
  minted token written beside it is dead on arrival and the operator sees the *same* fault
  after pressing the button. That is the worst possible outcome for a feature whose entire
  point is that the button works.
- **The log is deliberately not cleared on recovery.** The re-flashed board's first
  `ff-agent` line is a `boot` milestone, which clears the fault by S0-fe-4's existing rule —
  so clearing the log would be redundant on success and destructive on failure, where the
  evidence is the only thing S0-fe-7 will have to bundle.
- **`halted:` is a GENERIC hint, which corrects the remedy table the plan shipped with.**
  `park()` logs its reason strictly *after* the failure it reports, so a specific
  classification replaced "this token is single-use, flash the board again to mint a fresh
  one" with the engineer-facing "re-flash ff_cfg with a fresh ffe_ token (POST
  /v1/enrollment-tokens)" on the flagship case. Same derivative shape as `Backtrace:` and
  `SW_CPU_RESET` — the third time that pattern has been the right answer. It is, however,
  the one generic hint that **does** carry a remedy: the ban on the others exists because
  their fix belongs to the specific line above them, whereas re-flash is the same action
  whatever evidence names it, and a board that parks with nothing diagnosed above it (`no
  usable ff_cfg partition`, `no eFuse MAC`) has no other line to carry the button.
- **Gotcha that would have cost an afternoon: Testing Library matches accessible names by
  substring.** `Re-flash this board` contains `flash this board` and turns every existing
  `getByRole('button', { name: /flash this board/i })` into "found multiple elements"; a
  second `Reboot the board` breaks the EN-pulse test the same way. The labels are therefore
  `Re-flash the board` and `Reboot and retry`, pinned in `REMEDY_LABELS` so the panel and
  the tests cannot drift.
- **`flash()` stopped reading the `chip` state and reads `chipRef` instead.** `reflash`
  calls `connect()` and `flash()` in the same tick, and the state has not re-rendered — the
  engine would have selected the bundle for whichever board was on the desk last time, and
  an S3 bundle flashed to a C3 erases cleanly and never boots. `setChip` stays; the view
  renders from it. This is the one change in the task with a real cost when wrong.
- **At most one action on screen**, fault first and the overdue banner only when the fault
  has none. Two identical buttons are confusing to the operator and an ambiguous
  `getByRole` for every future test.
- **A missing user gesture now says something true.** `requestPort()` needs transient user
  activation and the recovery click awaits `release()` first; Chromium's `SecurityError`
  for a closed activation window used to map to "the page must be on HTTPS or localhost",
  which is wrong and sends the operator nowhere. Do not "fix" the underlying race by
  calling `onReflash()` without awaiting the release — the flasher would then open a port
  the console still holds and get `InvalidStateError` most of the time.
- **The loop closes itself and no code makes it happen.** The recovery flash drives `phase`
  through `flashing` → `done`, so `autoWatch` toggles, the panel's `armed` ref resets, and
  `watch('granted')` re-opens the port and pulses EN (S0-fe-5). Nothing was added for it;
  do not add anything.

Details: `docs/features/enrollment.md` → *Recovery is a button (S0-fe-6)*.

## 2026-09-11 — the console resets the board itself so the boot is never missed (S0-fe-5)

The panel's automatic reset is not a fix for a dropped session — it is the *designed
behaviour*. The flasher and the console are separate sessions at different bauds, and the
window between `hard_reset` and the console opening at 115200 is long enough (up to 8 s on
native-USB parts) to lose the entire boot. So every `watch()` pulses EN once, turning "the
board was silent when we arrived" into "the board prints its first line while we are
listening".

- **The pulse happens on every path that opens the port, automatic or manual.** A board
  watched five minutes after flash has the same silence; a purely-automatic pulse would leave
  the manual case (click **Watch a board** without flashing first) broken. Both paths pulse.
- **`commandedReset` travels in-band as an event property, not as a second argument.** The
  reboot-loop suppression depends on knowing which boot the panel asked for.
  `summarizeConsole` is pure over `events` alone (every test builds summaries from arrays,
  the hook memoises on `[events, now]`), so the flag rides in the event.
- **Order is load-bearing in `watch()`: append notice synchronously, attach the reader
  immediately, let the 150 ms EN pulse run concurrently.** Awaiting `session.reboot()` first
  would leave the port unread for 150 ms—Chromium's default 255-byte read buffer is ~22 ms
  at 115200 baud, so the panel can overrun. Appending the notice *after* the pulse makes its
  position in the stream racy, breaking the one-boot-deep suppression.
- **A failed pulse degrades to a notice, never to an error.** `setSignals` can be
  unsupported or wired differently. The operator sees a log region with "could not reset the
  board. Press 'Reboot the board'", never a red fault panel.
- **Native-USB re-enumeration is acknowledged but not solved here.** C3/C6/S3 drop off the
  bus on reset and return as a new `SerialPort`. The disconnect message now says "dropped off
  the USB bus when reset — some boards re-enumerate. Press 'Watch a board' to pick it up
  again." Automatic re-acquire is S0-test-2, blocked on hardware.
- **Deliberate residual: a board mid-OTA-download gets restarted.** The panel has no way to
  know an OTA is in progress (the agent prints no such line), and waiting to find out would
  recreate the silence problem. Acceptable at R0 (the console is a bench tool, and an
  interrupted OTA is discarded rather than committed). Documented so it's not later reported
  as a mystery.
- **Gotcha for the next console change: the summary is now a function of the clock.**
  `useBoardConsole` ticks once a second while watching (not idle). In tests, `vi.useFakeTimers()`
  + `vi.advanceTimersByTimeAsync()` inside `act()` drives both the tick and `Date.now`, which
  is the only way to test a deadline with no new line arriving.

Details: `docs/features/enrollment.md` → *The boot appears automatically (S0-fe-5)*.

## 2026-09-11 — a milestone is a claim about now; a fault is a record of what happened (S0-fe-4)

Implements the first layer of the standard set by the entry below. Three things were
decided while fixing it that are not obvious from the task text.

- **The parser must match the whole line, not the message.** `hintFor` was keyed on the
  ESP-IDF tag, which is why `E BOD:` was invisible. The fix could have been "add a rule for
  tagless lines"; instead every bare rule is matched against the **cleaned whole line**, so
  the same rule fires whether ESP-IDF prints `E BOD: …` early-boot style or `E (403) BOD: …`
  through the normal logger. A classifier keyed on a *format* is what broke; keying the
  content match on the format again would rebuild the same trap one layer down.
- **`reached` is cleared by a reboot and `fault` is not, and that asymmetry is the point.**
  A milestone is a positive claim that must be true *now* — a stale ✓ actively misdirects,
  which is precisely what cost the bench session. A fault is a description of something that
  happened; the reset it caused does not make it untrue, and a panic prints its cause
  immediately *before* the reset that would otherwise erase it. So progress clears a fault
  (unchanged) but a boot boundary does not.
- **"Twice is proof of a loop" needed a second clause, or the happy path cries wolf.** The
  filed task said seeing `boot` twice proves a reset loop. Literally true on a stranded
  board, false the moment the operator presses **Reboot the board** on a healthy one — and
  S0-fe-5 is about to make the panel reboot boards by itself. `rebootLoop` is therefore
  raised only when a boot begins while the *previous* boot had not reached the fleet, and is
  cleared by one that does. Same detection on the failure case, silent on the success case.
- **A generic hint is the right answer more often than it looks.** `Backtrace:` and
  `SW_CPU_RESET` both *look* like specific diagnoses and are both consequences printed after
  the line that actually names the cause. Classifying them `kind: 'generic'` reuses the
  existing "never overwrite a named cause" rule instead of adding ordering logic. Worth
  reaching for whenever a line is real but derivative.
- **Deadlines are grounded in the agent's own constants, not chosen.** `NET_TIMEOUT_MS` 30 s,
  `SNTP_TIMEOUT_MS` 15 s, `ENROLL_TIMEOUT_MS` 30 s, each plus room for one retry, and each
  measured from the *previous* milestone. A deadline shorter than the board's own patience
  would report a fault the board has not had yet. The numbers are not in `spec/prd.md`'s
  targets table; proposed for it rather than written there.
- **Gotcha for whoever tests the next layer:** the summary is now a function of the clock, so
  `useBoardConsole` ticks once a second **while watching only** — an idle panel must not
  re-render forever. In jsdom, `vi.advanceTimersByTimeAsync` inside `act()` drives both the
  tick and `Date.now`, which is the only way to test a stall that arrives with no new line.
- **The bench log is a reconstruction, and the fixture says so in its header.** The real
  capture was pasted into a chat and never committed. That is not a documentation lapse to
  be tidied up; it is the exact failure S0-fe-7 exists to remove, so it is recorded rather
  than glossed.

Details: `docs/features/enrollment.md` → *The console always names a diagnosis (S0-fe-4)*.

## 2026-09-11 — the console panel is the diagnostic surface of record for onboarding

The first hardware bench found no server bug and three onboarding bugs. A DevKit v1
brownouts during Wi-Fi PHY calibration and resets forever; it printed `E BOD: Brownout
detector was triggered` on every cycle, and the panel showed a stale green **Network
up** instead. The fault was found by pasting a UART log into a chat window.

Nothing server-side could have helped — a board that never associates is invisible to
`device_progress` by construction, which `progress.py` already says out loud. The
board's UART is the only witness and the browser is the only listener. So the panel,
not the server, is held responsible for explaining everything between "flashed" and "on
the fleet", against a technician who does not know what a brownout is. Anything the
board says that the panel cannot explain is now a defect.

Gates R0, because R0's stated risk *is* onboarding. Full reasoning and the rejected
alternative (structured faults from firmware) in
`design/decisions/enrollment-console-is-the-diagnostic-surface.md`; requirements in
`spec/standards.md`; the parser-versus-firmware trade and the missing CUJs in
`spec/open-questions.md`.

## 2026-09-11 — `arrivals` needed a second clause, not a tweak (S0-fe-3)

Closes the item filed at the end of the S0-fw-1 entry below. The question was whether "not
currently online" should also exclude boards that have *been* in the fleet, and the answer
is yes — but the interesting part is what the second clause had to be made of.

- **"Not online" was never the right question; "has this board already arrived?" is.**
  A board on its way up and a board that came up an hour ago and lost power are both
  offline with a recent stage. One clause cannot separate them, and the symptom was a
  completed arrival re-entering the list labelled *stalled at `mqtt_connected`* for the
  rest of the 900 s window, duplicating an offline row directly above it. The new predicate
  `progress.has_already_arrived` asks the second question, and it lives in `progress.py`
  next to `stalled` rather than inline in the router, so there is one place that decides it.
- **`broker_provisioned_at`, not `enrolled_at`.** The task's suggested shape named both.
  `enrolled_at` is `NOT NULL` with a default, so testing it decides nothing — a conjunct
  that is always true reads like a safeguard and is not one. The provisioning timestamp is
  the real end of the arrival sequence.
- **The re-flash case works for free because `last_seen` is monotonic and re-enrolment does
  not touch it.** Two decisions made elsewhere and for other reasons — `registry.py` leaving
  `last_seen` alone on re-enrol, `ingestor/store.py` advancing it with `GREATEST` — mean a
  re-flashed board's fresh stages are *necessarily* newer than its stale `last_seen`. So
  `stage_at <= last_seen` distinguishes "this stage belongs to the arrival that already
  finished" from "this board is arriving again" without a re-flash flag, a generation
  counter or a new column. **Worth noticing as a pattern: when a new rule needs to tell two
  situations apart, check whether an existing monotonic timestamp already does it.**
- **A deliberate residual, so nobody reports it as a regression.** A board that reports
  `mqtt_connected` and dies before any live message advances `last_seen` past that report
  still reads as arriving. It never completed a heartbeat. Fixing it would require deciding
  how many messages count as "arrived", which is a worse rule than the honest edge.
- **The live vacuity check found nothing but is why the evidence is trustworthy.** Neutering
  the clause in the running api reproduced the original duplicate row verbatim against the
  same database state that had just shown `arrivals: []`. That is a stronger statement than
  a passing test, because it proves the empty list came from the rule rather than from the
  progress window having quietly expired.

Details: `docs/features/enrollment.md` → *An arrival that finished stops arriving (S0-fe-3)*.

## 2026-09-11 — the stage reporter has now run on a board, and one acceptance was wrong (S0-fw-1)

Supersedes the 2026-09-10 S0-fw-1 entry below, which recorded the server half and said
`ff_progress.c` had exactly one guarantee: that it compiles. It has now been executed.
The design decisions in that entry all stand; these are what running it added.

- **Acceptance 1 asked for something the feature does not claim, and that is a spec bug,
  not a test bug.** "A board flashed with a deliberately wrong PSK shows a stalled stage
  rather than nothing at all" — but a wrong PSK means no link, and `ff_progress.h`'s
  header already states that a board with no route reports nothing. The criterion and the
  interface contradicted each other, and the interface is right. Owner-confirmed
  substitution: the two cases the feature *does* claim, a board stalled at `enrolling`
  (broker down, so `/v1/enroll` 503s on provisioning) and one stalled at `mqtt_refused`
  (credential rotated out from under it). Both now pass, driven by `ff_progress.c` rather
  than by curl. **When an acceptance and an interface disagree, check which one was
  written after the thing was understood.**
- **The invisible case is real, measured, and belongs in the operator's head.** A board
  whose token is refused reports *nothing*: the progress 401 self-disables the reporter,
  so the `halted` that `park()` tries to send never leaves the board. Verified with a
  revoked token — zero rows, silent dashboard, and only the `ff-progress` warning on the
  console. This is the designed behaviour and it is also the feature's ceiling: [[S0-fe-1]]
  (serial) and this task cover disjoint failures, and neither is a substitute for the other.
- **`progress_stall_s` (60 s) and `ENROLL_RETRY_MIN_MS` (60 s) are the same number, so a
  freshly-stalled board flickers.** The row alternates `stalled=false/true` for the first
  couple of minutes and only settles once the backoff has doubled past the threshold.
  Observed, not theorised. Left alone rather than tuned: the flicker is honest (the board
  *is* alternating between reporting and waiting) and changing either constant to fix a
  cosmetic wobble would trade a real property for a UI one. Worth knowing before someone
  reports it as a bug.
- **A trap that cost a full round of acceptance evidence:
  `docker ps --filter ancestor=espressif/idf:v5.5.5` matches nothing when the image is
  digest-pinned.** The filter compares the reference you typed, not the image the container
  runs, so it exits 0 having killed nothing — indistinguishable from "no emulators are
  running". Killing the `just` process does not help either: without `-it`, `docker run`
  leaves the container alive. Six emulators accumulated, all claiming `000000000000`, all
  enrolling and heartbeating over each other, and the first set of results had to be
  thrown away. Fixed at the source rather than in a doc: `agent-qemu` names its container
  `ff-qemu-<target>` and **refuses to start a second one**, and `agent-qemu-stop` exists.
  Vacuity-checked both ways. **Apply the general rule: if a cleanup command can fail
  silently, the thing it cleans up needs a name.**
- **A board is visible as `arriving` before it exists in the fleet at all** — the
  `link_up` arrival lands before any `devices` row does. That is the whole point of the
  feature and it is worth stating as an observed fact rather than an intent.
- **Filed, not fixed: an offline board reappears in `arrivals`.** Once presence decays,
  a board that got all the way to `mqtt_connected` and then died shows up as "arriving,
  stalled at `mqtt_connected`" for the rest of the 900 s window, duplicating a row the
  fleet list already shows as offline. The `arrivals` rule (recent stage + not online) is
  doing exactly what it says; whether "not online" should also exclude boards that have
  *been* in the fleet is a dashboard decision, so it is **S0-fe-3** rather than a quiet
  change here.

Details: `docs/features/enrollment.md` → *Boot & enrol stage reports (S0-fw-1)*,
`docs/runbooks/agent-qemu.md` → *Reproducing a board that gets partway*.

---

## 2026-09-11 — the QEMU harness was never broken; it was unrunnable and unverifiable (S0-infra-1)

- **The filed root cause was wrong and the named suspect is innocent.** S0-infra-1
  reported a `LoadProhibited` boot loop inside `esp_task_wdt_init` and suspected the
  `-global driver=timer.esp32.timg,property=wdt_disable,value=true` flag. It did not
  reproduce: a full run from a wiped flash image and a fresh token reached `enroll 200`
  → `mqtt connected` → heartbeats, and five further boots — three under eight busy-loops
  on a four-core box — produced zero panics. Every one of those runs carries the flag.
  **Recorded so nobody re-decodes that backtrace**: `docs/runbooks/agent-qemu.md` →
  *What we know about the boot-loop panic*.
- **`docker run -it` in a recipe is a bug, not a convenience.** `agent-qemu` passed it
  unconditionally, so the recipe died with "cannot attach stdin to a TTY-enabled
  container" in every agent session, script and CI shell — i.e. it could not be run by
  the things that most need to run it, including the acceptance criterion of the task
  filed against it. Whoever hits that hand-rolls a `docker run`, and a hand-rolled
  emulator invocation is where a wrong `-M`/`-m`/`-global` and an inexplicable watchdog
  panic come from. This is the most probable origin of the reported backtrace. `-it` is
  now conditional on `[ -t 0 ]`. **Apply this to any recipe that shells into a
  container.**
- **A digest pin does not pin what you run. Docker verifies a digest on `pull`, not on
  `run`.** A damaged or replaced local layer is used in silence, and
  `qemu-system-xtensa` lives inside the pinned image — which was in fact *absent* from
  this box's store when the investigation began and had to be re-pulled, with `/` at
  85%. Since every other input to a boot is content-addressed (bundle sha256s, IDF's own
  `default_efuse` bytes, `esptool merge_bin` over manifest offsets), identical declared
  inputs produced different behaviour, so one input was not what it claimed. New
  `qemu_sha256` hashes the emulator **binary** inside the container before every boot and
  prints its version into every transcript. Limit stated rather than papered over: one
  binary is not the whole image.
- **The two QEMU recipes splice one `qemu_program` definition.** A smoke check that
  assembles its own machine guards a lookalike, not the recipe. Everything that varies
  arrives as an environment variable so the argv cannot drift.
- **`just agent-qemu-smoke` — the cheap answer to "is the harness alive?"** ~17 s, no
  token, no stack, no board. Asserts only the first seconds, most-specific first: no
  panic, exactly one ROM `rst:0x` banner, the `ff-agent` banner, `ff_cfg v1 loaded`. It
  deliberately proves nothing about enrolment or MQTT. **This task cost a decoded
  backtrace and a blocked firmware task to answer a question worth seventeen seconds.**
  Vacuity-checked both ways: a wrong `qemu_sha256` trips the integrity guard, and 256
  scribbled bytes in `app.bin` produce a real loop — `the board reset 27 times`.
- **Consequence for [[S0-fw-1]]: it is unblocked, and its firmware has already run.** The
  bundle in `agent/dist/esp32` is the S0-fw-1 build (`ff_progress` in `app.bin`,
  `source_commit f81d6f1` + dirty tree) and it boots and enrols. The two firmware
  acceptances it could not reach are now executable on this box.

Details: `docs/features/infrastructure.md` → *The QEMU harness, re-verified*,
`docs/runbooks/agent-qemu.md`.

---

## 2026-09-10 — the theme is monochrome, so every state encodes twice (S0-fe-2)

- **One hue ramp means colour is no longer available as a carrier of meaning, and that is
  a functional consequence rather than a stylistic one.** On this palette green and red
  are the same grey. So `.ok` and `.bad` both go bright + bold and `.bad` additionally
  underlines; `.warn` goes body-weight + bold; `.muted` stays dim + normal. Every state
  in the app now differs from its alternative on at least two of {glyph, weight,
  lightness}. WCAG 1.4.1.
- **The audit came before the restyle, and it is why this touched one component.** Every
  `.ok`/`.bad`/`.warn`/`.muted` site was checked for whether colour was its *only*
  carrier. Nearly all already carried their own text — "active"/"used", "Live", full
  sentences, the checklist's `✓`/`…`/`·`, and log lines that begin with ESP-IDF's own
  `E (…)`/`W (…)`/`I (…)`. Exactly one was colour-alone: `FleetView`'s `StatusCell`,
  which drew the same `●` for online and offline. It is now `█` vs `░`. If a future
  change introduces a second such site, the audit — not the palette — is what has to be
  re-run.
- **`.log .bad` cancels the underline that `.bad` carries everywhere else.** Inside a log
  panel the level is already in the text, so the underline adds no information and
  destroys the monospace grid. A rule that exists to be *absent* in one place is worth
  the two lines of comment it has.
- **Two font stacks, and the split is load-bearing.** Press Start 2P is confined to short
  chrome (`h1`/`h2`/`h3`/`th`/`legend`/`button`); device ids, table data, inputs and both
  log panels stay on a real monospace at full size. The task's constraint was that a
  pixel font must not cost the legibility of the two things operators actually read, and
  a scoped display face is how that is *met* rather than hoped for. Do not unify them: a
  12-hex device id in Press Start 2P is ~2.5x wider and the face has no bold, so the log
  panel would lose its weight ramp too.
- **The font is self-hosted, latin subset only.** `@fontsource/press-start-2p`, 12 KB
  woff2, bundled by Vite and verified present in the production nginx image. A Google
  Fonts `<link>` would have been fewer characters and would have added a third-party
  fetch, a CSP consideration and a hard dependency on the box having a route out — for a
  single-tenant appliance whose V2 promise is self-hosting, that is the wrong trade.
- **The greyscale check runs as a live CSS filter on the page, not as a post-hoc PNG
  conversion.** Converting the screenshot only proves the screenshot is grey. Filtering
  at render time also catches anything that would have reintroduced hue — an accent, an
  emoji, a form control drawn by the UA. It passes trivially today precisely because the
  palette is achromatic, and that triviality is the point.
- **Gotcha, cost twenty minutes: a Docker named volume seeds from the image exactly
  once.** After adding the font to `frontend/package.json`, `just rebuild frontend` built
  a correct image that the stale `ff_node_modules` volume then masked, and Vite reported
  `Failed to resolve import` for a package plainly installed on the host. The volume must
  be *deleted*. The override file's comment said "rebuild after changing package.json",
  which is true and insufficient; it now carries the four-line recipe.
- **The frontend test suite cannot regress on a pure restyle, and it is worth knowing
  why.** Only `main.tsx` imports `index.css` and vitest never renders styles, so CSS is
  invisible to the 108 tests. The `StatusCell` glyph was the one change with any reach,
  and nothing asserts on it — the tests assert roles and labels. A restyle that *does*
  break a test has changed the markup more than it meant to.

Details: `docs/features/dashboard.md` → S0-fe-2 (new capability area, registered in
`docs/roadmap.md`). T2 harness: `frontend/scripts/theme-shots.mjs`.

---

## 2026-09-10 — a boot stage is reported over HTTPS under the enrollment token (S0-fw-1, attempted)

**Status: the server half shipped and is verified; the firmware half is written, compiles
and has never run.** S0-fw-1 stays `- [!]` in TODO.md for that reason. Read the last bullet
before trusting `agent/main/ff_progress.c`.

- **PROPOSED protocol addition, deliberately NOT written into `spec/`.** A board between
  "flashed" and "online" holds no MQTT credential — that is the thing it is trying to
  obtain — so a stage report cannot travel on MQTT. The only credential it has is the
  `ffe_` enrollment token it was flashed with, and the only channel is the HTTPS one
  `/v1/enroll` already uses. Hence `POST /v1/device-progress`, token in the body, `202`.
  `spec/device-protocol.md` is the frozen v1 wire contract and stays frozen until this is
  accepted; the endpoint's module docstring says so at the top so the two cannot silently
  diverge in the reader's head.
- **The token is verified and never burned, and that distinction is the whole security
  argument.** `auth.enrollment.BURN_SQL` is not imported by the progress router and must
  never be: a board reports `enrolling` several times before it succeeds, and a reporting
  path that spent tokens would turn a debugging aid into a way to strand boards.
- **An already-burned token is still accepted, but only from `used_by_device_id` and only
  before `expires_at`.** Without that exception the two most valuable stages — `enrolled`
  and `mqtt_connected`, which by definition happen *after* the burn — could never be
  reported at all. It widens nothing: the predicate names one device, and the endpoint
  issues no credential, provisions nothing and writes no `devices` row.
- **`stalled` is derived on read, never stored** (`progress_stall_s`, 60 s), in one
  function, exactly as `online` is derived by `presence.is_online`. A stored `stalled`
  would need a sweeper and would be wrong between sweeps.
- **`arrivals` rides on `GET /v1/devices` instead of getting an endpoint.** The dashboard
  already re-reads that on every `ff_events` hint, so arriving boards cost no second fetch
  and no poll. Arrivals are boards with a recent stage that are **not currently online**,
  decided by the same `is_online` call that fills the rows above them — so a board leaves
  the arriving list at the exact moment it really joins the fleet.
- **The table is bounded on write, not by a sweeper**: 20 rows per device, trimmed in the
  same transaction as the insert. A board retrying enrolment every 60 s reports forever,
  and a debugging table must not be able to outgrow the fleet it describes. Known bound,
  accepted: an unspent valid token can name any `device_id`, so it can seed rows for
  arbitrary ids at the rate limiter's ceiling — small rows, 24 h token TTL, single-tenant.
- **No PG enum and no CHECK on `stage`.** The R0 agent is flash-baked; the server must
  tolerate an agent it can never update, including one that invents a stage. The API
  bounds the string's *shape* (`^[a-z][a-z0-9_]{0,31}$`, no control characters in
  `detail`), never its vocabulary. `ProgressStage` in `db/models.py` is advisory, and
  `FF_PROGRESS_*` in `ff_progress.h` are plain strings for the same reason. Verified: an
  unknown stage (`teleported`) is stored and rendered as itself.
- **The SSE frame carries no `detail`.** `detail` is device-controlled free text; the
  event is a hint and the client re-reads, as for every other event type.
- **The honest limit is designed for, not around: a board with no route to the server
  reports nothing.** Stated in `ff_progress.h`'s header so nobody builds on a promise it
  cannot keep. This is for boards that get *partway*, and never a substitute for the
  serial console ([[S0-fe-1]]).
- **The firmware could not be run, and the harness is why.** `just agent-qemu esp32`
  boot-loops on a `LoadProhibited` panic inside `esp_task_wdt_init` before `app_main` —
  **reproduced at unmodified HEAD**, so it is not this change. The Mac is the flashing
  bench, so there is currently no way to execute agent firmware on this box at all. Filed
  as **S0-infra-1** with the decoded backtrace. Until it is fixed, `ff_progress.c` has
  exactly one guarantee: it compiles under `-Wall -Wextra -Werror`.

Details: TODO.md → S0-fw-1 (`- [!]`) and S0-infra-1.

---

## 2026-09-10 — the board's console belongs in the browser, as a second session (S0-fe-1)

- **`flash.ts`'s Rule 4 stays: the port is always released in a `finally`.** The task was
  filed as "keep the serial port after flashing", and that framing is wrong. esptool-js's
  `Transport` owns the port at the *flash* baud, which is not the console baud, so a held
  flasher would have to be reconfigured anyway — and unwinding the `finally` would leak a
  port on every error path in a file whose whole discipline is that it never does. The
  console is instead a **separate session against the same physical port**, opened after
  the flasher lets go.
- **It works with no user gesture because nothing ever calls `port.forget()`.** That rule
  was written into `esptoolFlasher.ts` for a different reason (not making the operator
  re-pick the chooser for each board), and it is what now lets an effect call
  `navigator.serial.getPorts()` after a flash and get the port back. Two features rest on
  that one line; do not "tidy" it away.
- **The classifier ranks hints by specificity, not recency.** First cut showed the most
  recent explained line. On a stranded board that is always `agent_main.c:167`'s "no
  network yet; waiting for the link", reprinted every 5 s, which buried the `reason 201`
  above it. Hints carry `hintKind: 'generic' | 'specific'`; generic fills an empty slot and
  never displaces a named cause. Caught by a test written from the real 2026-09-10 log —
  the value of using a genuine failure as a fixture rather than an invented one.
- **A milestone clears the fault.** Wi-Fi retries are normal on a busy AP; leaving "the PSK
  is wrong" on screen after the link came up would be a lie the operator would act on.
- **No inactivity timeout, and this is load-bearing.** `agent_main.c:166` retries forever
  at 5 s, so the failure mode has no window — any timeout would drop precisely the slow
  failure the panel exists to find. Documented in the interface, not just the code.
- **Hardware properties are a separate task, not a hand-wave.** Four things (port
  re-acquisition after `hard_reset` on native-USB parts, 115200 decoding, the EN pulse
  landing in the app rather than the ROM loader, `screen` getting the device after Release)
  are properties of a bridge chip and an OS and cannot be proven in jsdom. Filed as
  **S0-test-1** with the specific failure signature to look for in each, rather than left
  as an implied "should work". The bench is the Mac.
- **Every log string the classifier matches is quoted in `boardConsole.test.ts`.** It is a
  contract with `agent/main/*.c` that nothing else enforces: reword an `ESP_LOGW` and the
  panel would silently stop diagnosing. The tests fail instead.

Details: [docs/features/enrollment.md](docs/features/enrollment.md) →
*Serial console after flashing (S0-fe-1)*.

## 2026-09-10 — fleetforge goes live on prod: an alias network, an infra ingestor, a TLS simulator (R0-infra-5)

- **A dedicated `fleetforge` network exists solely to carry the alias `api`.** The
  frontend's nginx has `proxy_pass http://api:8000` compiled in, and the production box
  already runs a `content-api`. The alternatives were rebuilding the image with a renamed
  upstream (couples every future frontend build to one deployment's naming) or templating
  the nginx config at start-up (a whole mechanism for one string). A third network that
  only these three containers join is cheaper than both, keeps `backend` clean, and the
  next fleetforge image works unmodified.
- **The ingestor is `INFRA_SERVICES`, never `APP_SERVICES`.** `docker rollout` runs two
  copies during the swap; the ingestor is the fleet's sole MQTT subscriber, so that
  duplicates every telemetry row. Same reasoning that already keeps `mosquitto` out. This
  is a correctness constraint on the deploy script, not a preference — it is commented at
  both sites in `deploy.sh` and in the compose file.
- **The api migrates; the ingestor must not.** `RUN_MIGRATIONS=true` on one container
  only. Two processes racing `alembic upgrade head` deadlock on a slow migration.
- **Gotcha — `01-init.sh` is disaster recovery, not deployment.** `docker-entrypoint-initdb.d`
  runs only on an empty data directory. Adding the `fleetforge` role there created nothing
  on the running box; it had to be made by hand with `psql`. The file must be kept in sync
  with what was created manually, and now says so.
- **Gotcha — a missing smoke-test case rolls back a healthy deploy.** `--service fleetforge`
  had no entry in `smoke_endpoint_for_service`, so the check curled an empty URL, got HTTP
  000 and fired the auto-rollback while every container was in fact healthy. Adding a
  service to the filter without adding its endpoint is a trap; commented at the function.
- **Gotcha — argon2id in `.env` must be single-quoted**, or compose eats the `$argon2id`/
  `$v`/`$m` segments and login can never succeed. The R0-infra-3 hash had exactly this
  problem and its plaintext was unrecoverable, so the admin password was reminted here.
  Verify with `docker compose config | grep -i ADMIN_PASSWORD_HASH`; a literal `$$` there
  is correct.
- **The simulator learned TLS (`--tls`), verification only, no pinning.** Acceptance needed
  a board over `mqtts://…:8883` and R0-test-1 had left TLS as an explicit TODO. Off by
  default because the dev broker is plaintext behind Traefik. Worth knowing: without the
  flag against a TLS listener the connect does not error, it *hangs* — which reads like a
  firewall problem and sent the first attempt down the wrong path.
- **No object store, on purpose.** `constraints/iam.disableServiceAccountKeyCreation`
  blocks minting the GCS key and no R0 route touches the store. R1 is blocked on it;
  tracked in docs/runbooks/artifact-storage.md.

Details: docs/features/infrastructure.md → *The app on prod*.

---

## 2026-09-10 — Flashing from the browser: offsets, ordering and one more `ff_cfg` writer (R0-fe-3)

- **No offset is ever derived, only read.** Every address the flasher writes comes from
  `GET /v1/agent/manifest` — `builds[].parts[].offset` and `config_partition.offset`. The
  bootloader is at `0x1000` on ESP32 and `0x0` on the RISC-V parts; a hardcoded offset
  flashes cleanly and never boots, which is the most expensive failure this feature can
  have. `planWrite` is the single place a write is constructed.
- **The enrollment token is minted LAST and revoked on failure.** Validate → manifest →
  chip and flash-size checks → download and sha256-verify every part → *then* mint. It is
  a single-use fleet-join credential: minting first spends one on every failed attempt,
  and a live one baked into a half-flashed board is an orphan nobody is tracking. On any
  failure after the mint the flasher revokes it and says which id, never the plaintext.
- **The form re-runs the engine's own validation on every keystroke.** Not redundancy:
  `power=sleepy` with no wake interval is refused by `POST /v1/enroll` (422), and reaching
  that refusal costs a token. The Flash button is dead until the config would be accepted.
- **A third implementation of `ff_cfg` needed a shared golden vector.** `ff_cfg.py`
  (writer), `ff_cfg.c` (firmware reader) and now `frontend/src/ffcfg.ts` (browser writer)
  must agree byte for byte. `frontend/src/ffcfg.vector.json` carries fields plus the
  sha256 the **Python** writer produced for them, and both suites assert it from their own
  side, so neither writer can move alone. The vector is ASCII-only — `json.dumps` defaults
  to `ensure_ascii=True` and `JSON.stringify` does not, and that is the only region where
  the two are guaranteed to produce identical bytes.
- **`explainFlashError` belongs to the seam, not the adapter.** The commonest failure of
  all is `requestPort()` rejecting because the operator dismissed the chooser — thrown
  *before* an adapter object exists, so it is caught by the engine, which must not import
  esptool-js. Found in T2 against a real Chromium, where the page said `Failed to execute
  'requestPort' on 'Serial': No port selected by the user.` instead of "No board
  selected.". Related: `DOMException instanceof Error` is **true** in Chromium and
  **false** across realms (jsdom's), so the translator reads `name`/`message` off the
  value rather than testing `instanceof`.
- **Erase-on-by-default is a correctness setting.** A re-flashed board whose NVS still
  holds a broker credential reuses it (R0-fw-1: "reusing the stored credential"), so the
  freshly minted token baked into it is never spent and the board never re-registers.
- **`flashSize: 'keep'` costs us esptool-js's fit check, so we do it ourselves.** All three
  of `flashMode`/`flashFreq`/`flashSize` are `'keep'` to stop esptool-js rewriting the
  bootloader's flash-parameter byte and recomputing the image SHA; the price is that a
  2 MB board would silently accept the 4 MB A/B layout. `checkFlashable` derives the bound
  from the manifest — never the string `4MB`, never `ab-4m-v1`.
- **No MD5 read-back.** esptool-js can verify flash contents with MD5; Web Crypto has no
  MD5 and pulling in `crypto-js` to get one is the wrong trade. The sha256 of every part is
  verified against the manifest *before* the first byte is written, which catches the
  failure that actually happens (a truncated download), and esptool-js checksums every
  block on the wire.
- **esptool-js 0.6.1 has no `romBaudrate` option** (the plan assumed one). It is fixed at
  115200 inside `ESPLoader`; tutorials that pass one target a different major. `baudrate`
  is what the stub raises the link to after connecting.
- **The dev container's `node_modules` volume does not re-seed itself.** Adding
  `esptool-js` to `package.json` is invisible to the running Vite server until the named
  volume is removed (`docker compose stop frontend && docker compose rm -f frontend &&
  docker volume rm fleetforge_ff_node_modules && just up`) — otherwise the browser gets
  "Failed to resolve import", which reads like a code bug.

---

## 2026-09-10 — The live device list, and why a stream is not a source of truth (R0-fe-2)

- **The dashboard never patches a row from an event payload.** `GET /v1/devices` is the
  record; a frame on `/v1/events` only says "go re-read". The payload carries an `online`
  snapshot and using it is the obvious free optimisation — it is also how the table starts
  disagreeing with the server about a board. The frame is parsed only far enough to reject
  garbage, and is never rendered or logged.
- **The plain 10 s re-read is a correctness requirement, not a fallback.** A sleepy board
  goes offline with **no event at all** (presence expires on read, `2.5 × wake`, and
  publishes nothing). A purely event-driven list shows it as online forever and passes
  every test one would naturally write. `fleet.test.tsx` carries the test that fails if
  the interval is removed, and T2 proved it end to end: after the will, the event stream
  showed only `: keepalive`, and the row still flipped 29 s later.
- **`EventSource` cannot be trusted to notice a dead stream.** Found in T2, not review:
  behind the Vite dev proxy, `docker compose stop api` leaves the socket open and
  `onerror` never fires — the page said `Live` at a stream that was gone and did not
  recover when the api came back. Fix: the read model is the detector. A failed poll marks
  the stream suspect; the next successful read discards the source and rebuilds it. This
  is also the only reconnect path that survives a proxy holding a half-open socket.
- **CLOSED is not CONNECTING.** A network blip leaves `EventSource` CONNECTING and the
  browser owns the retry (`retry: 2000` from the server). A non-200 — 401, or
  `sse_max_clients` 503 — leaves it permanently CLOSED and needs a manual reconnect,
  1 s → 30 s, deliberately mirroring `RECONNECT_INITIAL_DELAY`/`RECONNECT_MAX_DELAY` in
  `api/eventstream.py`. Treating the two alike either hammers the API or hangs forever.
- **A dead API is not a dead session.** Only a 401 renders the login form; a transport
  failure keeps the last list under a banner. Extends the R0-fe-1 rule to a long-lived
  connection, where the temptation is stronger because the failure is continuous.
- **`just now` must be narrower than the heartbeat interval.** A 5 s "just now" bucket
  exactly swallowed the 5 s heartbeat: last-seen never moved, and a frozen column reads
  exactly like a page that has stopped updating. Single seconds, pinned by
  `format.test.ts`. Caught by running AC1, not by any unit test written before it.
- **The test seam is an injectable `EventSourceFactory`, because jsdom has no
  `EventSource`.** A structural interface a real `EventSource` satisfies without a cast —
  no polyfill, no new npm dependency, and the fake can drive `readyState` 0 vs 2, which is
  the distinction above.
- **`just frontend-test` is separate from `just frontend-build` and both are in `just
  build`.** Vitest config lives in `frontend/vitest.config.ts` because the frontend
  container never reads that file (see R0-fe-1); `npm run build` therefore cannot run the
  tests, so the pipeline runs them itself.
- **Details:** `docs/features/enrollment.md` → *Live device list (R0-fe-2)*;
  `frontend/src/fleet.ts` (the refresh engine and the three triggers);
  `.claude/plans/R0-fe-2-live-device-list-sse.md` (the plan).

---

## 2026-09-10 — What the prod box can actually hold (R0-infra-4)

- **Swap *used* is a stock, not a flow.** The TODO's "already swapping ~1 G" was a point
  sample, and it was already wrong (the box rebooted and swap-used dropped to 11 MB by the
  next measurement). A gigabyte of cold anonymous pages parked in swap and never read back
  costs nothing; what costs is the *rate* of `pswpin` / `pgmajfault`. A verdict built on
  "swap used is 1 G, therefore resize" would be wrong. A verdict built on window deltas
  over 15 minutes is defensible. This is the reusable insight.
- **`memory.events max` is the real under-provisioning signal.** It counts forced reclaims
  *at* the limit — which happen long before an OOM kill and are otherwise invisible. A
  container with `max > 0` is under-provisioned even if it never crashes. `memory.peak`
  alone is not enough; `docker stats` and `memory.current` both include reclaimable page
  cache. The harness reads cgroup v2 directly and reports both.
- **Measuring on dev in the production shape transfers.** Fleetforge's app is not on prod
  yet (R0-infra-5 is blocked on permissions), so the measurement splits: footprint of api /
  ingestor / frontend → dev box in the production shape (`just up-prod`: built images,
  nginx not Vite, same limits); host headroom → `prod` over a sustained window. Container
  RSS for these workloads is set by the workload, not the host. The projection is then:
  measured prod headroom − measured fleetforge footprint − margin. It is a projection and
  must say so; the harness is the acceptance instrument for R0-infra-5 to confirm it.
- **The shared-Postgres cost must be counted explicitly.** Fleetforge on prod does not
  bring its own Postgres — it adds a database and connections to the shared `postgres-prod`
  (1 GiB limit, 199 MiB in use pre-fleetforge). Every backend is ~5–10 MB of private RSS:
  api pool connections × api processes, plus one dedicated `LISTEN` connection per API
  process (`application_name='fleetforge-events'`, from R0-be-5), plus the ingestor's pool.
  This is not in the 512 M declared-limit table and must be measured directly rather than
  estimated.
- **Verdict for R0: no resize needed.** The 151 MiB measured footprint (131 MiB app + ~20 MiB
  marginal Postgres) fits in the 384 MiB headroom bingo freed. Declared over-commit is
  99.5% (3904 / 3924 MiB MemTotal), but measured peaks are what matter and the net add is
  negative. Follow-up: re-run `just capacity-check-prod` after R0-infra-5 lands to confirm
  this projection against live measurements.
- **Details:** `docs/runbooks/capacity.md` (how to re-run it, what the numbers mean, the
  resize procedure); `design/production.md` → *Capacity — Measured 2026-09-10* (replaces
  the stale sample with the as-built measurement + verdict);
  `.claude/plans/R0-infra-4-prod-capacity-check.md` (the plan).

---

## 2026-09-09 — The connect-only agent, and how it is proved without hardware (R0-fw-1)

- **`ff_cfg` is a CRC-headered JSON blob, not a struct.** 16-byte little-endian header
  (`FFCF`, version, reserved, payload length, CRC32) followed by compact UTF-8 JSON,
  0xFF-filled to the 4 KB partition reserved by R0-infra-2. A packed C struct would be a
  second wire format to version, and the flasher that writes it is a **browser**
  (R0-fe-3) — JSON is the one encoding both ends already have. The CRC is what turns a
  half-written partition into one refusal line instead of a board that connects
  somewhere unexpected. `agent/tools/ff_cfg.py` and `agent/main/ff_cfg.c` are the two
  ends of that contract and `tests/test_ff_cfg.py` holds them together (it greps the C
  source for every key the Python writer emits).
- **The config keys are `ssid`/`psk`/`mqtt_pass`, and the spelling is not cosmetic.**
  `tests/test_agent_partitions.py::test_agent_holds_no_credential` fails the build if
  anything under `agent/` puts `wifi_password`, `mqtt_password` or an `ffe_…` literal
  next to a quoted value. That tripwire is worth more than pretty names, so the names
  moved. Where a long name was unavoidable (`ff_enroll.c` parsing the enroll response)
  the literal is split — `"mqtt_" "password"` — with a comment saying why it must not
  be "tidied".
- **`ff_net` is a seam with two adapters, and that is what makes a hardware-free T2
  possible.** `ff_net_wifi.c` is what ships on a board; `ff_net_openeth.c` drives QEMU's
  OpenCores NIC and compiles to a refusal stub wherever `CONFIG_ETH_USE_OPENETH` is off.
  `link` in `ff_cfg` picks one at runtime. Same idiom as the local/GCP seams in the
  Python side.
- **`CONFIG_ETH_USE_OPENETH=y` lives in `sdkconfig.defaults.esp32` only.** The emulated
  NIC exists on no real board and on no other target; the common defaults file stays the
  one place the safety posture is read from.
- **`CONFIG_MBEDTLS_HAVE_TIME_DATE=y` — the one that was silently missing.** ESP-IDF
  defaults it **off**, and with it off mbedTLS never looks at `notBefore`/`notAfter`: an
  expired certificate validates, and `spec/device-protocol.md` → *Clock — SNTP before
  TLS* describes a failure that cannot happen. Found by AC5 doing the opposite of what
  the plan predicted — a board with a 1970 clock completed a real TLS handshake against
  `bingo.tvaroska.sk`. Enabling it makes the spec's rule true, and costs a board whose
  SNTP never answers its TLS channels (accepted; the SNTP client keeps retrying in the
  background while the enroll ladder backs off). `verify_bundle.py` and
  `test_agent_partitions.py` both require it now, in the **resolved** config.
- **`CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y`, no pinned CA.** Both channels terminate at
  Traefik with a Let's Encrypt certificate (R0-infra-3), so the Mozilla root bundle is
  the trust store. Proved in QEMU against the real production hostname.
- **The confirm call is guarded by `ESP_OTA_IMG_PENDING_VERIFY`, and R0 must not "fix"
  that.** `esp_ota_mark_app_valid_cancel_rollback()` runs only for an image the
  bootloader is actually watching; a serially flashed board never enters that state, so
  the timer is inert today. Calling it unconditionally at boot would compile, look
  correct, pass every R0 test — and disable R2's auto-rollback on the entire fleet.
- **No goodbye publish.** A board that is dying cannot send one. Presence-off is the
  broker's job via a retained LWT, which is what `/v1/devices` and the dashboard already
  key off (R0-test-1 established the same posture for the simulator; the simulator sends
  a goodbye because it is a process, not a board).
- **The token stays in `ff_cfg` after it is spent.** Erasing it would mean writing to a
  partition the firmware otherwise only reads, on every first boot, to remove a string
  that is already dead server-side. The credential in NVS is what stops a second
  enrollment, and `ff_store_load()` distinguishes *absent* from *corrupt* so a torn write
  parks the board instead of burning another token.
- **QEMU's eFuse MAC is all zeros, so every emulated board is `000000000000`.** The agent
  warns once per boot and does not paper over it: `device_id` is the eFuse MAC on real
  silicon and there is no special case anywhere in the code. Two emulators are one device
  as far as the fleet is concerned.
- **`.qemu/` is a credential directory, not a cache.** 0700, gitignored: `ff_cfg.bin`
  holds a live single-use token and `flash-esp32.bin` holds, inside NVS, the broker
  password that board was issued. QEMU writes the image back (`if=mtd`), which is exactly
  what makes "a reboot burns no second token" testable — and what makes `--fresh` a
  credential deletion.
- **Gotcha, ~30 min:** the dev stack routes by `Host`, and the emulated board addresses
  this box as slirp's `10.0.2.2` — so every enroll came back **404 from Traefik**, having
  never reached the API, which reads exactly like a firmware bug. Fixed with a dev-only
  `ff-qemu` router in `docker-compose.override.yml` (and `10.0.2.2` added to Vite's
  `allowedHosts`, which rejects unknown Hosts for the same reason).
- **Gotcha:** `just` drops empty arguments when it splices `*args` into a recipe, so
  `--ntp ''` cannot be expressed through `just agent-cfg`. "No NTP" is therefore a flag
  (`--no-ntp`) — a test knob for the clock rule, not a setting.
- Details: `docs/runbooks/agent-qemu.md` (worked transcript), `docs/features/enrollment.md`,
  `.claude/plans/R0-fw-1-esp32-agent-connect-only.md`.

---

## 2026-09-09 — Agent firmware: what is frozen at flash time (R0-infra-2)

- **`ab-4m-v1` is frozen, and it is a three-way contract.** `agent/partitions.csv`
  (`ota_0`/`ota_1` at `0x1E0000`), `spec/device-protocol.md`
  (`"ota_slot_size": 1966080`, `"partition_layout": "ab-4m-v1"`) and
  `tests/test_agent_partitions.py` (which retypes both literally and greps the spec)
  move together or not at all. A partition table cannot be changed by OTA, so a new
  layout is a NEW ID plus a server that understands both — never an edit to this one.
- **No `factory` partition, deliberately.** A factory-only board can never OTA its way
  to an A/B layout. `make_manifest.py` refuses to emit a bundle whose built table has
  one, is missing `ota_1`, or whose slots differ in size.
- **`ff_cfg` (data, subtype `0x40`, 4 KB @ `0x12000`) is reserved now, defined later.**
  It is where the browser flasher will write the per-board broker URL, Wi-Fi credentials
  and enrollment token. `R0-fw-1`/`R0-fe-3` own the payload format; reserving the space
  after boards ship is impossible, so it is reserved before anything ships.
- **Rollback on, eFuses untouched.** `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` is safe to
  enable at R0 because only an OTA'd app enters `PENDING_VERIFY` — a serially flashed one
  never does, so nothing can brick before `R0-fw-1` exists. Anti-rollback, secure boot and
  flash encryption stay **off**: they burn eFuses per board, irreversibly, and there is no
  key-management story yet. `verify_bundle.py` fails the build if any appears enabled in
  the RESOLVED config.
- **Offsets are read from ESP-IDF, never typed.** `make_manifest.py` takes them from
  `build/flasher_args.json` by name. The bootloader really is at `0x1000` on ESP32 and
  `0x0` on the RISC-V parts; a hardcoded value flashes cleanly and never boots on half
  the fleet.
- **The ESP-IDF pin is a digest; bumping it is a decision, not a version bump.** It
  changes the bootloader and app on every board flashed afterwards while fielded boards
  keep the old one. Procedure in docs/runbooks/agent-build.md.
- **Agent bundles are baked into the app image, not routed through `ObjectStore`.** They
  are build outputs that version with the image, identical for every tenant, ~1.2 MB per
  target — and the prod GCS credential cannot currently be minted at all
  (`constraints/iam.disableServiceAccountKeyCreation`, docs/runbooks/artifact-storage.md).
  Putting them behind the object store would take a working feature and make it
  unshippable. R1's *user* artifacts still go through `ObjectStore`.
- **`fleetforge-agent-*` images are NOT compose services.** Never add them to
  `PULL_SERVICES`/`APP_SERVICES`: `docker compose pull` fails as a unit (see the
  R0-infra-5 entry below). They exist as a provenance handle; production gets the bytes
  from the app image.
- **Gotcha, cost ~40 min:** `espressif/idf:v5.5.5` unpacks to **~8.9 GB**, not the ~5.5 GB
  estimated. The pull dies with `failed to register layer: no space left on device`.
  Reclaim with `builder prune -af` / `container prune -f` / `image prune -f` and
  regenerable caches only — **never** `image prune -a`, `system prune -a` or
  `volume prune` on this box, which holds other projects' images and 31 volumes.
- **Gotcha:** matching forbidden sdkconfig options by PREFIX rejects every correct esp32
  build — `CONFIG_SECURE_BOOT_V1_SUPPORTED=y` is a SoC capability symbol, not an
  enablement. Exact names only. A safety check that fails on correct input teaches the
  next person to delete it.
- **Gotcha:** a project-root `sdkconfig` silently overrides `sdkconfig.defaults` from the
  first build onward, so a committed one would ship a bootloader whose posture no longer
  matches the tracked defaults. Gitignored and dockerignored; builds run in a container.
- **ESP-IDF builds are not byte-reproducible, and that is why provenance is in the
  manifest.** `esp_app_desc_t` embeds the compile date/time, so rebuilding the same commit
  with the same pinned toolchain yields a different `app.bin` sha256 (the partition table
  and otadata are stable). A registry pull of a pushed digest IS byte-identical — verified
  push → `rmi` → pull by digest → export → `diff -r`. `CONFIG_APP_REPRODUCIBLE_BUILD=y`
  would fix the rebuild case but changes every binary, so it is a separate decision.
- **Gotcha:** in IDF 5.x `build/config/` holds only generated `.h`/`.cmake`/`.json`
  views — the text sdkconfig is at the project root. Resolve it from
  `project_description.json["config_file"]`; a literal path breaks on the next IDF bump.

## 2026-09-09 — One app image serves both the api and the ingestor (R0-infra-5)

- **Two images, not three.** `fleetforge` runs the api and the ingestor; they are
  the same code with a different `command`, exactly as `docker-compose.yml`
  already builds them from one `fleetforge:dev`. A separate ingestor image would
  rebuild identical layers and give the pair a way to drift in production.
  `fleetforge-frontend` stays separate — different base, different build.
- **`just build` runs the T1 gate before it pushes.** Prod pulls by tag, so a
  broken build reaching the registry is a production defect, not a local one.
- **The frontend image cannot be verified with `nginx -t`.** nginx resolves
  `proxy_pass http://api:8000` at config load, so the syntax check fails with
  "host not found in upstream" anywhere there is no api container. That is the
  same real behaviour that forces the prod api service to carry the network alias
  `api` — but it makes `nginx -t` unusable as a standalone gate. `_verify-images`
  asserts the built payload instead (non-empty `index.html`, an `assets/*.js`).
- **The ingestor must never enter `APP_SERVICES`.** `docker rollout` runs two
  copies during the swap, and the ingestor is the sole MQTT subscriber
  (design/production.md → *The single-subscriber rule*): two would double every
  telemetry row and split the SSE audience. Same reason mosquitto is excluded.
  It goes in `INFRA_SERVICES`, recreated in place.
- **Gotcha:** never add a name to `PULL_SERVICES` before the compose file defines
  it. `docker compose pull` fails as a unit, so an unresolvable ref breaks the
  deploy for every other app on the box.
- The prod fragment half is not shipped — see docs/features/infrastructure.md.

## 2026-09-09 — TLS for the fleet terminates at Traefik, not at the broker (R0-infra-3)

- **The shared Traefik owns 8883.** A `mqtt` TCP entrypoint with a
  ``HostSNI(`bingo.tvaroska.sk`)`` router and `tls.certresolver=myresolver`
  terminates TLS and forwards **plaintext** to `mosquitto:1883` on an internal
  network. The alternative — TLS passthrough, or certs mounted into the broker —
  would need DNS-01 or a second renewal path for one service. HTTP-01 over :80
  already issues the certificate; the TCP router just reuses it. Nothing about
  ACME changed, and the broker container knows nothing about TLS.
- **`prod/mosquitto/` in the `services` repo is a copy, and the copy is guarded.**
  `deploy.sh` only ships `services/prod/`, so the config has to live there. `acl`
  is the entire fleet authz model, so `validate-config.sh` now diffs the two trees
  and fails the deploy on drift. A copy nobody checks is how a stale ACL reaches
  production.
- **Prod broker usernames must not look like device ids.** `ff-admin` /
  `ff-ingestor`, not 12 lowercase hex digits — the `acl_file` patterns key on `%u`,
  and `ensure_client` does create → already-exists → `setClientPassword`, so a
  hex-shaped service username could be re-keyed by enrolling that `device_id`.
  This is the R0-sec-1 reviewer note, now honoured in `services/prod/.env`.
- **The broker is excluded from `docker rollout`.** Rollout runs two copies during
  the swap; two brokers cannot share the dynsec store or the 1883 bind. New
  `INFRA_SERVICES` list in `deploy.sh` recreates it in place instead.
- **Gotcha, cost ~25 min:** with the fleetforge dev stack running on this machine,
  `just deploy` fails its staging gate with a **504 on every smoke test while all
  containers report healthy**. The staging Traefik sees the host daemon through
  socket-proxy and picks up `fleetforge-frontend`'s ``Host(`localhost`)`` rule,
  which outranks staging's `PathPrefix(/)`, then cannot reach that network. It
  reads exactly like a content-api regression. `docker compose stop` in fleetforge
  first. See docs/features/infrastructure.md → *R0-infra-3*.

## 2026-09-09 — The dashboard trusts the server's token status (R0-fe-1)

- **The frontend never recomputes token state.** `status` comes from the API, which derives it
  from the burn predicate itself (`auth/enrollment.token_status`). Recomputing it in the browser
  from `expires_at`/`used_at`/`revoked_at` looks trivial and is the bug: the two implementations
  drift, and the dashboard eventually shows "active" for a token that `POST /v1/enroll` will
  refuse — which reads, in the field, as broken enrollment rather than a stale token.
- **The issued plaintext lives in React state and nowhere else.** No `localStorage`, no
  `sessionStorage`, no URL, no error message. The server cannot re-derive it, so persisting it
  "for convenience" would be storing an un-rotatable fleet-join credential in the most readable
  place in the browser. Three tests in `EnrollBoard.test.tsx` and one browser-context assertion
  exist purely to fail if this regresses.
- **R0-fe-1 had to ship the login gate.** The task line says "generate token", but the token
  endpoints are admin-authenticated (R0-be-1), so the page was unreachable in a browser without
  one. Scope grew by a screen; the alternative was a page only `curl` could use.
- **Session state is not mirrored client-side.** The cookie is HttpOnly, so the page cannot read
  it; "signed in?" is `GET /v1/auth/me` plus a 401 watch on every later call. A mirrored boolean
  would only ever disagree with the cookie. A transport error is explicitly not treated as a
  logout — that distinction stops an operator re-typing the admin password at a dead API.
- **Gotcha, cost ~15 min: `vite.config.ts` is loaded by the container, so it may not import test
  deps.** Putting the `test` block there (via `defineConfig` from `vitest/config`) broke the
  `frontend` service with `ERR_MODULE_NOT_FOUND`, because the image's `node_modules` has no
  `vitest`. Traefik then dropped the unhealthy backend and every `/v1/*` call returned a bare
  **404** — a symptom that points at routing, not at a config import. Vitest config now lives in
  `frontend/vitest.config.ts`, which the container never reads.

## 2026-09-09 — The simulator is a device, not a test fixture (R0-test-1)

- **`python -m fleetforge.simulator` imports nothing from the server.** No `fleetforge.config`
  (and therefore no mandatory `DATABASE_URL`), no `fleetforge.db`, no `fleetforge.api` — only
  `fleetforge.identity` for `DEVICE_ID_RE`. A simulated board that needs the server's database URL
  to boot is modelling the wrong thing, and the import would drag the ORM into a process
  pretending to be an ESP32. This is a deliberate deviation from `broker/__main__.py` and
  `storage/__main__.py`, both of which do `Settings()`; there is an AST tripwire test so the
  deviation cannot rot back.
- **Retain flags are the contract, and a wrong one is a silent wrong answer.** `announce` and
  `presence` retained, `hb` **not** — a retained heartbeat would be replayed on every ingestor
  reconnect and `handlers.py` treats a retained message as a replay, so `last_seen` would quietly
  stop advancing. The LWT is retained too, or a server restart never learns a board is dead.
- **A clean disconnect does not fire the LWT**, so Ctrl-C on an `always_on` board would leave it
  online forever in the dashboard. The simulator publishes a retained `{"online":false}` goodbye on
  any clean shutdown (which is what a planned reboot should do anyway), and `--crash-after` uses
  `os._exit(1)` — a TCP FIN with no DISCONNECT — as the *only* honest way to exercise the will.
  aiomqtt has no public API for dropping a connection and `client._client` is private.
- **A sleepy wake is a fresh `aiomqtt.Client`**: entering the same client twice raises
  `MqttReentrantError` (2.5.1). Sleep is a clean disconnect rather than a simulated brownout; the
  server-visible state is identical because `is_online` ignores `presence_reported` for sleepy and
  the ingestor never advances `last_seen` on `presence:false`. Verified live: after the last wake
  the board stayed `online:true` for ~25 s (2.5 × 10 s) and then flipped with **no** message and
  **no** SSE event — presence is computed on read.
- **The device id is a locally-administered pseudo-MAC** — `(sha256(name)[0] & 0xFE) | 0x02` — so a
  simulated board is stable across runs, can never collide with a real Espressif OUI, and can never
  reach the `ffff…` ids `broker/__main__.py` reserves. Provable, and proven in a test, rather than
  unlikely.
- **The broker password is written to `.sim/<device_id>.json` at 0600, gitignored, never logged.**
  It is the NVS analogue and it exists nowhere else — same posture `spec/prd.md` already states for
  a real board. State present means **no enrollment happens**, because tokens are single-use; a
  corrupt state file is a loud error rather than a silent re-enroll that burns one.
- **Everything is validated before the token is presented** — the same ordering rule
  `POST /v1/enroll` follows internally. A `sleepy` board with no wake interval fails before the
  HTTP call, not after the burn (confirmed against a live stack: the token stayed `active`).
- **`fleet` issues its own tokens** through `POST /v1/auth/login` → `POST /v1/enrollment-tokens`,
  because typing three single-use tokens by hand is the friction this task exists to remove. Token
  **ids** are printed, plaintexts never are.
- **The agent connects with `client_id = device_id` and `clean_session = false`**, implementing the
  proposal R0-be-4 recorded: command durability comes from the persistent session, never from a
  retained `dn/cmd`, and MQTT 3.1.1 requires a non-empty client id for one. Three additive
  `spec/device-protocol.md` changes follow from this work and are **proposed, not written**: the
  LWT is published with `retain = true`; the agent's client id and clean-session flag; and a
  planned shutdown SHOULD publish a retained `{"online":false}` before disconnecting.

---

## 2026-09-08 — Broker authz: dynsec authenticates, acl_file authorises (R0-sec-1)

- **Mosquitto 2.0's dynamic-security plugin does not support `%u`/`%c` substitution.**
  Verified against 2.0.22: a `device` role holding `publishClientSend ff/v1/d/%u/up/#`
  denies the very client it names (MQTT v5 PUBACK 135), while the same role with a literal
  topic allows it, and the plugin binary contains no substitution code. `%u` is `acl_file`
  syntax. Every earlier document that says the two pattern ACLs live in a dynsec role — the
  R0-be-4 plan, `broker/provisioner.py`, `CRITICAL.md`'s wording — was wrong about the
  mechanism, not about the model.
- **So the two mechanisms are split: dynsec = authentication (who exists, what password,
  written by `/v1/enroll`), `acl_file` = the fleet ACL (the two `%u` pattern rules,
  verbatim from `spec/device-protocol.md`).** Mosquitto consults both and **allow wins**,
  proven in both directions. The spec's promise — "no per-device ACL rows, nothing to
  provision at enrolment" — survives intact; only the file it lives in changed.
- **The dynsec `device` role exists and is empty.** `createClient` requires a role name
  (`broker.DEVICE_ROLE`), and a name mismatch answers 503 on every enrolment. Moving the
  pattern rules into it does not fail loudly — it silently denies the whole fleet.
- **Read authorisation is enforced on delivery, not on SUBSCRIBE.** With `acl_file` a device
  may subscribe to `#` and gets SUBACK 0, then receives only its own `dn/` traffic (verified).
  Any test that asserts on the SUBACK code proves nothing. Same class: a forged LWT is
  accepted at CONNECT and dropped when it fires, so presence cannot be forged for another
  board (verified — the ingestor logged nothing for the impersonated device).
- **A denied publish is invisible below MQTT v5, and the broker log does not help.**
  aiomqtt/paho surface only the local `rc`, and 3.1.1 has no reason code at all. Mosquitto
  2.0.22 logs `Denied PUBLISH` at `MOSQ_LOG_DEBUG`, which `mosquitto.conf` does not enable
  (debug logs every topic — not worth the noise). The authoritative check is
  `mosquitto_pub -V 5 -d` **inside** the broker container, reading the PUBACK: `RC:135` is
  the denial, `RC:0`/`RC:16` are both "allowed" (16 only means nobody was subscribed, so an
  allowed publish reads as `RC:0` whenever the ingestor is up). `just broker-check` asserts
  on non-delivery instead, which is what the Python client can actually see.
- **`dynamic-security.json` is mutable state in the data volume, owned by uid 1883.** The
  plugin rewrites it on every enrolment; a root-owned file logs "not writable", applies the
  change in memory, and loses every device credential at the next restart — with the API
  reporting success. The bootstrap chowns and chmods it, and `docker compose logs mosquitto
  | grep -c "not writable"` is an acceptance check.
- **Bootstrap runs a throwaway broker inside a one-shot init container.** `mosquitto_ctrl
  dynsec init` is the only file-mode subcommand; everything else needs a live broker. The
  script is idempotent (create → "already exists" → `setClientPassword`), so it is safe on
  every `up`, and the broker `depends_on` it with `service_completed_successfully`.
- **The healthcheck authenticates as the dynsec admin**, the only client that exists before
  the bootstrap and the only one `dynsec init` gives `$SYS` read. The topic must be
  single-quoted (`'$$SYS/broker/uptime'`); a broken broker probe shows up as a Traefik 404,
  not as an unhealthy badge.
- **The ingestor gets its own credential and its own read-only role**
  (`subscribePattern` + `publishClientReceive` on `ff/v1/d/+/up/#`, no `$SYS`, no write).
  It is a different privilege from the API's dynsec admin and rotates separately.
- **Devices enrolled during the `NullProvisioner` era cannot be reconciled.** The broker
  password only ever existed in the enrolment response; the server cannot re-provision one
  the device would know. They must re-enrol with a fresh token — documented in
  `docs/runbooks/dev-stack.md` rather than built as a command that cannot work.

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
