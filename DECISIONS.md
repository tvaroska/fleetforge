# Decisions

Append-only log of product/technical decisions and learnings. Newest first.
Each entry: what was decided, why, and where the details live. Never rewrite
history — supersede an old decision with a new entry that references it.

---

## 2026-10-05 — The fleet row says where a board is or was ("on: shed" / "offline, last on: shed", "knows N networks"), never why it is offline; the card names "none of its N known networks is in range" from the console (R2b-fe-13)

**Decided: frontend only. The note lives in the Status cell, copy in `network.ts`.**

- **Placement.** After the presence word in the Status cell, with a muted `knows N network(s)` line. Seven columns stay, `StatusCell` untouched.
- **Copy.** `on:` online, `last on:` offline (with a title: the server cannot tell out of range from powered off). Null ssid renders nothing. Count shows whenever reported. `link_type` is not consulted.
- **Count asymmetry.** The result card's Link row shows the count only from 2 networks up; the fleet row from 1.
- **Classifier.** The two 0.4.6 cycle lines are specific `wifi` hints, no remedy (the board keeps trying). Reason 201 on `; trying "x" next` / `; that was the last known network` is generic, otherwise the fault flips every cycle.
- **Facts, not fault.** The card reads `ConsoleFacts.unjoined` (set by the cycle lines, cleared by link up / joined / boot), so its copy is stable.
- **Gotcha.** SSID is device-controlled; U+202E passes the server filter, so it renders in `<bdi>`.
- **Rejected:** a Network column; "unknown" for null; deriving from `link_type`; changing `summarizeConsole`; a remedy button; a server-side "out of range" state.

Supersedes nothing.

---

## 2026-10-05 — The flasher writes up to four networks, one `<form>` per network marked username/current-password; Ethernet writes no Wi-Fi fields; nothing is stored (R2b-fe-12)

**Decided: `ffcfg.ts` writes `nets` (networks 2..N) and validates it exactly as `ff_cfg.py::_validate_networks` does; `FlashBoard.tsx` has up to 4 network rows. Frontend plus `tests/test_ff_cfg.py`; no agent, backend, spec or `session.tsx` change.**

- **Format.** `KEY_ORDER` gains `'nets'` last (Python `KNOWN_KEYS` order), which removes the strict xfail in the TS key test. `MAX_NETWORKS = 4`, `MAX_SSID_BYTES = 32`, `MAX_PSK_BYTES = 64` retyped and tripwired. One network writes today's bytes (no `nets` key, never `"nets": []`); an empty entry passphrase omits `psk`. Second golden vector `ffcfg.nets.vector.json`, digest from the Python writer.
- **Every extra row counts.** A blank added row is refused ("network 2 needs an SSID, or remove it"), never silently dropped, so message numbering equals form numbering. Messages say "network N" (top level is 1) and never contain a value. The TS also gained the top-level psk ≤ 64 bytes check it lacked.
- **Ethernet writes no Wi-Fi fields** (small intended change: a leftover passphrase used to be baked in). The encoder still accepts `nets` on ethernet, as Python does.
- **Password manager.** One `<form noValidate onSubmit=preventDefault>` per row (Chrome treats a form with 2+ password fields as sign-up/change-password), SSID `autocomplete="username"`, passphrase `current-password` (`new-password` would offer a generated one and suppress fill). `autocomplete="off"` removed (Chrome ignores it for passwords). The app still writes nothing to any storage.
- **Autofill hazard.** The admin login is a password-only form on the same origin, so Chrome may offer/fill the admin password into a Wi-Fi field, which would be baked into the board's ff_cfg (readable over USB). Pre-existing (Chrome ignored `off`); mitigated by a hint under the rows. `session.tsx` untouched (CRITICAL, admin auth).
- **Old-agent warning.** ≥ 2 networks and a known build older than `NETS_MIN_AGENT = '0.4.6'` (`agentReadsNets`, numeric, suffix ignored, unparseable → no warning) shows a non-blocking warn line.
- **Result card / bundle.** `flashed.ssid` is null with several networks; `consoleFacts` reads `joined "<ssid>" (known network K of N)`. The bundle prints `networks  N known: …` after `link` on wifi when N > 1 (as `ff_cfg_log()`), keeps network 1's passphrase length only, and scrubs every passphrase.
- **emit-ffcfg.** Repeatable `--net SSID [PSK]`; prints `nets=<K networks, passphrases not shown>`; a parse error no longer echoes the argument.
- **Gotcha.** `flash.ts` appends `token` after the built fields, so the real blob's key order is `…, nets, token`, not `KEY_ORDER`. Harmless (JSON), left as is; the byte-exact proof is on `buildFfCfgFields` + `encodeFfCfg`.
- **Rejected:** dropping blank rows; one form for all rows; `new-password`; `navigator.credentials.store(PasswordCredential)`; a show/hide toggle labelled with "passphrase" (ambiguous `getByLabelText`); editing `session.tsx`.
- **Proposed follow-ups, not filed:** a hidden `autocomplete="username"` field (value `admin`) on the login form so the two credentials are distinguishable (CRITICAL, admin auth); `PasswordCredential.store` if the bench shows Chrome never offers to save.
- **Not proven here:** Chrome's actual save/fill bubble (headless has no password-manager UI; rides on R2b-test-4 / R2b-test-1) and a real Wi-Fi join of network 2 (R2b-test-4).

Supersedes nothing.

---

## 2026-10-05 — The board's ssid and known_networks are stored as last reported, written as a pair on every announce; malformed is null, never a lost announce (R2b-be-5)

**Decided: `devices.ssid TEXT NULL` + `devices.known_networks SMALLINT NULL` (migration `0005`). Both edges, `up/announce` and `POST /v1/enroll`, normalise through one never-raising module, `src/fleetforge/announce_fields.py`. `DeviceSummary` exposes both right after `link_type`.**

- **A pair on every announce (the exception to "absent means no change").** `store.py::_network_values` always writes both. If a key is absent or null, NULL is written. The spec reads absent and null as "not reported", and the announce is the full identity, republished every session. A board that OTAs back to a pre-0.4.6 agent, or is re-flashed onto ethernet, must not keep a stale "on: shed". "Last on: shed" for an offline board still works, because nothing clears the pair between announces. Enroll follows the same rule: `IDENTITY_FIELDS` overwrite on re-enrolment with `identity.get(field)`. The `if not values:` read-only branch in `apply_announce` can no longer be reached from an announce. It is kept as a guard.
- **Malformed is NULL and never an error.** A `ValidationError` in `AnnouncePayload` makes `decode()` drop the whole announce, `fw_version` included, and the retained copy repeats that drop on every reconnect. The spec forbids refusing an enroll over these fields. So `mode="before"` validators coerce (the `StatusPayload._plausible_pct` pattern), and a malformed value can never become a 422, before or after the burn.
- **Rules.** `ssid`: a `str` of 1-32 UTF-8 bytes (802.11, = `ff_cfg.py::MAX_SSID_BYTES`). No `Cc` character, and it must encode to UTF-8. No strip and no case change, because it is stored "exactly as written in ff_cfg". `""` is "not reported" and logs nothing. `known_networks`: an `int` that is not a `bool`, in `0..64`. 64 is a plausibility ceiling, not the protocol cap of 4, so a future writer that raises the cap is still stored. Floats and strings are not coerced. No cross-field rule: an `ssid` on ethernet is stored as reported.
- **Logging.** One INFO line per malformed field: `device <id|?> announced an unusable <field> (<reason>); stored as null`. Never the value, because an SSID is operator data. On MQTT the id is the payload's `device_id` and is printed only if it is canonical, since board text must not reach the log. A payload without one prints `?`. The topic id is not available inside the model.
- **Where.** `fleetforge.announce_fields` sits outside both `api` and `ingestor`, because the API may not import the ingestor (the `identity.py` precedent). R2b-be-6 adds the board measurements there.
- **Schema.** Nullable, no default (no row is rewritten), no CHECK. Both edges normalise first, and a CHECK would turn a normaliser bug into an `IntegrityError` that loses the announce. `downgrade()` drops only device-reported data, which the next announce re-supplies.
- **Read model / frontend hand-off.** The `DeviceSummary` fields default to `None` only so that no other constructor breaks. `_device_summary` always sets them, and `deploy` stays last. **`frontend/src/api.ts` is unchanged on purpose.** R2b-fe-13 adds `ssid: string | null` and `known_networks: number | null` after `link_type` in its mirror, along with the fixtures. Until then the client ignores the extra keys.
- **No event change.** `device.announce` already prompts the dashboard to re-read. The SSID stays out of SSE envelopes (same rule as `name`, R2b-be-1).
- **Simulator.** `DeviceIdentity` always announces both, in spec order. `--ssid` / `--known-networks` exist. A wifi sim with no flags announces `sim-wifi` / 1, and other links announce what the flags say. There is no `validate()` rule, so junk can be sent to test tolerance.
- **Gotchas.** A NUL in TEXT makes PostgreSQL raise and roll back the transaction. `json.loads('"\\ud800"')` returns a lone surrogate that asyncpg cannot encode. `True` is an `int`. All three are tested at both edges.
- **Deploy-order gotcha (release).** The ingestor's ORM selects and returns `devices.ssid`, so a new-image ingestor on a `0004` schema fails every message (`UndefinedColumnError`). That includes the retained replay it does on connect. `ingestor/main.py` assumes "a database the api has already migrated", but `services/scripts/deploy.sh` runs `up -d $INFRA_SERVICES` (ingestor included) **before** `docker rollout fleetforge-api`, which is what migrates. This is the first migration to touch a table the ingestor maps, so it is the first time the order matters. Seen on dev during T2: about one minute of errors, cleared by restarting the ingestor after the migration. At release, restart `fleetforge-ingestor` after the api rollout. A durable fix (migrate before recreating the ingestor, or have the ingestor wait for alembic head) belongs to the `services` repo / a follow-up task.
- **Rejected:** adding the fields to `ANNOUNCE_FIELDS` (it skips `None`, which leaves stale values); clearing only on `link_type == "ethernet"` (a downgraded agent would still be stale); a strict `str`/`int` that raises; DB CHECKs; changing `api.ts` in a backend task; logging the rejected value.

Supersedes nothing.

---

## 2026-10-05 — The agent joins the first known network its scan sees, by fixed priority, and announces `ssid` and `known_networks`; the link adapter is started once (R2b-fw-1)

**Decided: agent 0.4.6 reads `nets` into `ff_cfg_t.nets[4]` (`[0]` is the top-level ssid/psk), selects by fixed priority in `ff_net_wifi.c`, and emits `ssid` / `known_networks` right after `link_type`. Patch B of R2b-spec-1 is applied (+2 lines in the `up/announce` example), nothing else under `spec/`.**

- **Reader (`ff_cfg.c::parse_nets`).** Absent or null `nets` is nothing. Malformed (not an array, an entry that is not an object, a missing/empty/non-string `ssid`, a non-string `psk`, over-length `ssid` > 32 / `psk` > 64 bytes) idles the board with `config nets[N] is malformed — the board idles until it is re-flashed`. Past 4 networks in all, entries are counted, not validated, and the board boots with `config lists N networks; this agent keeps the first 4 and ignores the rest` (forward compatible with a writer that raises the cap). Lenient on purpose: `nets` with no top-level `ssid` builds the list from `nets` alone and warns that agents older than 0.4.6 cannot join with it (the spec does not list it as malformed; writers refuse it). No de-duplication in the reader.
- **Log.** Every existing `ff_cfg_log` line is unchanged for an old blob. A list adds one line: `networks  N known: home, shed, bench` on wifi (SSIDs only), `networks  N in ff_cfg, unused (link is ethernet)` on ethernet, which is the QEMU proof that the parser ran. The passphrase line still prints only `nets[0].psk`'s length; no other passphrase length is printed.
- **Selection (`ff_net_wifi.c`).** One network: the old path, byte for byte (same config, connect on STA_START, no scan), plus `no known network in range (1 known); trying again in N s` on reason 201. Several: one cycle is one scan, then every network the scan saw in list order (`WIFI_ALL_CHANNEL_SCAN` + `WIFI_CONNECT_AP_BY_SIGNAL`, so the strongest AP of that SSID), then the unseen ones directly (hidden SSIDs), then one console line (`no known network in range (N known); scanning again in N s`, or `none of the V known networks in range could be joined (N known); …`), then the 1 s → 30 s backoff. Between networks inside a cycle the wait is a fixed 1 s; the backoff doubles only between cycles. A scan is started only from `begin_cycle()`, reached from STA_START and the DISCONNECTED paths, never while associated; a working link is never left. A one-shot 20 s **DHCP watchdog** (esp_timer, multi-network only) drops an association with no address so the next network is tried; it checks `s_have_ip` first and is disarmed on GOT_IP. Scan results are read one record at a time (`esp_wifi_scan_get_ap_record`, never an array on the ~2.3 KB event-loop stack) and `esp_wifi_clear_ap_list()` is reached on every SCAN_DONE path, so no sdkconfig change was needed.
- **TX ladder.** Only failures against a network the scan saw move it; a direct try of an unseen network does not (lower power never finds an absent AP). The ladder freezes on association as before.
- **Start-once fix (`ff_net.c`).** `agent_main.c` retries `ff_net_bring_up()` every 30 s + 5 s, and each call used to re-run the adapter's `*_start()` (a second `esp_netif_create_default_wifi_sta()` / `esp_wifi_init()` / handler registration) and clear the GOT_IP bit, losing an address that landed between calls. A multi-network cycle routinely exceeds 30 s, so this would have become routine. The adapter is now started once (`s_started`, set only on ESP_OK so a failed first start is retried) and the bit is cleared only before that first start; later calls only wait. Also fixes a single network behind an AP that boots slower than 30 s. QEMU behaviour unchanged.
- **Announce.** `ssid` comes through the seam (`ff_net_ssid()` → `ff_net_wifi_ssid()`, `ff_identity.c` never includes `ff_net_adapter.h`); both fields are null on ethernet; never a passphrase or another network's SSID. The enroll body carries them too (same object).
- **Console phrase for R2b-fe-12/13.** The classifier should key on the prefix `no known network in range (`. The existing contract (`ff-wifi`, `disconnected (reason N)`, `carries no ssid`, `associated; waiting for DHCP`, `wifi sta starting`) is byte-identical; context is only appended after it.
- **Writer (`agent/tools/ff_cfg.py`).** `--net SSID [PSK]` (repeatable), `MAX_NETWORKS = 4`, `MAX_SSID_BYTES = 32`, `MAX_PSK_BYTES = 64` retyped against `ff_cfg.h`. `validate()` refuses everything the reader idles on, plus more than 4, `nets` without a top-level `ssid` (whatever the link) and duplicate SSIDs; messages name an index (`nets[2].psk`), never a value. `describe()` hides nested `psk`. `nets` with `link=ethernet` is allowed. `KNOWN_KEYS` gained `nets`; the browser writer does not write it yet, so that one param of the TS key test is a strict xfail that R2b-fe-12 must remove.
- **Agent 0.4.6.** Flash budgets for all four targets raised to the measured bytes (c3/c6 had been stuck at 0.2.0-era numbers). An OTA'd 0.4.6 that joins nothing never confirms and rolls back unattended (the confirm timer, untouched, is still first in `app_main`).
- **Not proven here.** QEMU has no radio: the scan/selection loop, the DHCP watchdog, the in-range line on real radio and strongest-AP choice are R2b-test-4 (bench, hardware-gated).
- **Rejected:** a lock around the selection state (all writers are on the event-loop task); a de-duplicating reader; idling on an over-long list; counting unseen direct tries on the TX ladder; a sdkconfig change for the event-task stack.

Supersedes nothing.

---

## 2026-10-04 — The R2 bench replay is one ordered session graded from deploy_events by a judge; "silent store" on metal is a WAN cut; the run is held for the board (R2b-test-2)

**Decided: `scripts/bench_judge.py` + `just bench-judge` grade each bench step; `docs/runbooks/bench-replay.md` is the session script. `R2b-test-2` stays open until a human runs it at the bench.**

- **Why it is held.** The acceptance is a person at the Windows bench with the S3 `94a990dd09a4` on COM3 pulling USB and switching an AP off. Blocked on `S0-bug-1` (board offline since 14:24 UTC), the board's agent (0.3.x; every step needs ≥ 0.4.5: 0.4.0 for `rolled_back` from the returned-to image, 0.4.3 for a hang image to roll back, 0.4.4 for the stall guard; via `S0-infra-10` or a normal deploy), and a prod upload path (R2b-fe-7 `392bf28` is not an ancestor of prod `9200e0f`; the runbook's curl `POST /v1/artifact` is the fallback). Same precedent as R2b-test-1.
- **The judge reads rows, not the API.** `GET /v1/devices` carries only the newest transaction's summary. The judge pipes one `SELECT … FROM deploy_events … ORDER BY at, id` into psql **on stdin** (read-only over ssh on prod, `docker compose exec` on dev). REF is a 32-hex cmd_id or a 12-hex device id (newest `requested`), lowercased; anything else exits 2 before any SQL exists, and the recipe takes its arguments as env vars (`$scenario $ref $where`), never spliced into the script. Exit 0 PASS, 1 FAIL, 2 usage/refused/no rows, 3 INCOMPLETE. Deviation from the plan: the SELECT also returns `cmd_id` (7 columns), so a device-id run prints the cmd_id the operator must write down and re-deploy.
- **Scenarios** (one spec each, one checker; order is a subsequence; the first row must be `requested`): `confirmed`; `rbtest` (`staged, confirming, rolled_back`, `rolling_back` best-effort WARN, 60-240 s from `rebooting` when present); `bootloop` (no `confirming`/`rolling_back`, ≤ 180 s only from a `rebooting` row); `hang` (artifact ≥ 0.4.3, ≥ 300 s, > 900 s WARN); `power-cut` (open `downloading`/`verifying` is the pass, never INCOMPLETE); `outage-short`; `stall` (`failed` with exactly `download stalled` or `download failed`, and it says which); `reboot-outage-long`. A rollback needs the agent's detail `returned to ota_N; ota_M did not confirm`; the simulator's wording fails it by design.
- **"Silent store" on metal is a WAN cut with the AP up** (plus R2, the AP off). "Alive but silent" needs a proxy in the board's path, so it and the pre-1 KB residual stay QEMU-only.
- **Dashboard `apply: auto` only at the bench.** The dashboard has no on_command; on metal `esp_restart()` works and auto means no step is blocked by a parked staged image.
- **No recipe or script builds fault images.** `test_no_recipe_builds_a_fault_image` is the tripwire; the runbook has hand-run `docker build` lines into `/tmp/ff-bench-*` and a copied context for `0.4.5-bench`, so `agent/` never goes dirty.
- **Trap.** `/tmp/ff-hang-esp32s3` is `0.4.2-hangtest` (built at `5fa5c33`), the negative control: the judge fails it by name. The version gate reads the label, so it cannot catch an old build given an arbitrary label (`/tmp/ff-hang-esp32` is pre-0.4.3 code labelled `0.4.22-hangtest`); build the bench images at HEAD.
- **T2-R evidence (dev).** Images: esp32s3 `0.4.5-bench/-rbtest/-bltest/-hangtest` at `1a00f7e`, all `verify_bundle.py` OK, one `config_sha256` (`d10f52d6…`, equal to `agent/dist/esp32s3`), sha256s in the runbook snapshot. Wiring: unknown device exits 2 "no rows"; `x';--` exits 2 refused, docker never invoked (PATH shim). Simulator: `0f4e8e45…` `JUDGE PASS confirmed`; the same judged as bootloop `JUDGE FAIL`; `57d00633…` (9.9.9-rbtest, `--confirm never --confirm-timeout 70`) `JUDGE FAIL rbtest` with only `rollback-detail` failing (the designed negative control). QEMU esp32, artifacts uploaded with the runbook's curl: `938acbb8…` `JUDGE PASS power-cut` at 30%, then re-POST `reused:true` same cmd → `JUDGE PASS confirmed`; `38262d37…` `JUDGE PASS bootloop` (one `abort()`, window SKIP: no `rebooting` row); `176b907c…` `JUDGE PASS hang` (staged → rolled_back 323.9 s). Prod read-only pipe ran: the board's newest txn is the R1-era `0.3.2-rbtest` deploy parked at `rebooting` → `JUDGE FAIL confirmed`, as expected.
- **Rejected:** a judge over the API; a just recipe or script that builds fault images; a proxy at the bench for "alive but silent"; flipping the task on a rehearsal.

Supersedes nothing.

---

## 2026-10-04 — "Send again" is Deploy pinned to the failed version: it opens the pre-check, never posts; offered only on a failure before reboot that the same build can survive (R2b-fe-11)

**Decided: `deployResult.sendAgain` names the version; `DeployCell` runs the normal pre-check for it. Frontend only.**

- **D1.** Nothing reaches `/deploy` without the pre-check card (R2b-fe-8). The board may have changed since the failure; the pre-check is where that is said. The click selects the failed version (even when the select defaulted to a newer one) and opens the card; the operator clicks Send.
- **D2.** Offered only for `failed`, steps known and none past the reboot, known version, and `resend: true` on the failure entry. `resend` is explicit on every entry (false for `artifact larger than the ota slot`, `image validation failed`), and a test pins `resend === /send it again/i.test(next)` so copy and flag cannot drift.
- **D3.** The cell also requires the version to be in the board's artifact list, and disables the button while `phase !== 'idle'`.
- **D4.** `DeployResultCard` takes optional `onSendAgain` and `busy` (capability by prop); plain button, no role or aria-live, not inside a `.bad`/`.warn` paragraph.
- **D5.** The rollback card keeps "Do not send X again as it is" and has no button.
- **D6.** No change to `deploy.ts`, the timeline, status strip, `api.ts` or backend.
- **Rejected:** a one-click direct POST; offering it on plain `failed` or unknown steps (unknown is not "before"); deriving `resend` from the copy.

Supersedes nothing.

---

## 2026-10-04 — Who sent a deploy is a snapshot on the `requested` row's `detail.sent_by`; no column, no migration (R2b-be-4)

**Decided: `deploys.record_requested` writes `detail.sent_by = {subject, token_id, credential}`; the API surfaces `{subject, credential}` as `DeploySummary.sent_by`; the result card shows a `Sent by` row.**

- **What.** `subject` is `AuthContext.subject` (`admin` in v1). `token_id` is the public half of the `ffa_` token, the same id the deploy log line prints (`requested by <uuid>`), so an auditor can join the record to the log; stored, not exposed. `credential` is the token's `name` at send time (`dashboard session (<client ip>)`).
- **Snapshot, not a join.** `deploy_events` is kept forever and must be readable without joining; token rows have their own lifecycle. If the token row is gone, `credential` is null; an audit label never fails a deploy.
- **Original sender stands across reuse.** A repeat POST inside the reuse window writes no row, so the transaction's sender is whoever opened it. The table stays append-only, one row per intent.
- **The IP is a label, not proof.** It is the leftmost validated `X-Forwarded-For`, client-spoofable.
- **`deps.py` untouched.** It is the admin-auth path (CRITICAL.md); the router reads the token name with one `session.get(AdminToken, ...)` in the same session, only on the non-reuse branch and after the refusal checks.
- **Read side.** A third bounded query (not the steps query, which is capped at the newest `MAX_DEPLOY_STEPS` and could drop the `requested` row). `detail -> 'sent_by'` decodes to a Python `dict` through SQLAlchemy `text()` on asyncpg, verified by the devices tests. Parsed defensively: non-dict or empty/non-str `subject` gives null. It never enters `DeploySummary.detail` or `steps`.
- **Rejected:** a `sent_by` column and migration; a read-time join to `admin_tokens`; adding `name` to `AuthContext`; a second row on reuse.
- **Gotcha.** `tests/test_agent_power_and_size.py::test_every_built_bundle_is_within_budget` fails on this box (local `agent/dist` esp32c3/esp32c6 bundles are over budget); unrelated and untouched here. The dev admin password in `.env` did not match; T2 recreated the api with the `.env.example` hash and restored it afterwards.

Supersedes nothing.

---

## 2026-10-04 — The unaided re-run waits for the R2b flow on prod; the software half was rehearsed in a real Chromium (R2b-test-1)

**Decided: `R2b-test-1` stays open. The unaided run is held until the R2b flow is on prod; this pass delivers the run script and a software rehearsal, not the acceptance.**

- **Why it is held.** The acceptance needs a person who has not seen the code, the S3 `94a990dd09a4` on the Windows bench, and the new flow on prod. Prod is 0.4.2 (`9200e0f`) and none of the R2b commits are on it (`git merge-base --is-ancestor 4b06281 9200e0f` is false); prod's flasher serves agent 0.3.2 (esp32s3) / 0.2.0 (others) against the repo's 0.4.5 (`S0-infra-10`); and `S0-bug-1`'s power-cycle diagnosis must happen before the run, because the re-flash erases its evidence. A release and a publish are prod changes that need the owner's go-ahead.
- **The two runs.** Run 1 is a known-board re-flash of `94a990dd09a4` (it is already enrolled, so it also exercises the pre-flight card and the re-enrol path). Run 2 uses a wrong Wi-Fi passphrase as the induced fault. A spent token is impractical to induce now: every flash mints a fresh token. Script: `docs/runbooks/unaided-onboarding.md`.
- **The software half.** `frontend/scripts/onboarding-rehearsal.mjs` runs the console and result card (Flow 1 steps 5-6) in a real Chromium against the dev stack, replaying real agent log lines through a fake `navigator.serial`, six scenarios graded on the S0-test-3 bar. It is run by hand, not in `npm test`, and Playwright is resolved from `PLAYWRIGHT_MODULE`, never a package dependency (same rule as `theme-shots.mjs`). 6/6 pass; it is a proxy and does not cover detect, flash or a real USB re-enumeration.
- **Gotchas.** The dev admin password in `.env` is not necessarily `fleetforge-dev-only`. `just rebuild <svc>` fails at its `up -d` on a box with pruned images (it pulls `minio/mc`); after the build use `docker compose up -d --no-deps --no-build <svc>`. The dev API and frontend containers predated R2b (the PATCH route, fe-3 to fe-6) and were rebuilt to HEAD first.

Supersedes nothing.

---

## 2026-10-04 — A board is named from the success card; name only, no group picker; 422 details are made readable once in detailOf (R2b-fe-6)

**Decided: `NameBoard.tsx` hangs off the success result card and sends `PATCH /v1/devices/{id}` with `{"name": ...}` only. Frontend only; no backend, migration or spec edit.**

- **Name only, no group picker (1).** There is no `GET /v1/groups` and group CRUD is V3, so a picker would have nothing to list. The group/tag half of the flows.md line is deferred. The body is exactly `{"name": ...}`: never `group_id`, because merge-patch reads an explicit `null` as "ungroup".
- **Where it shows (2, 3).** Only on the success card, only when `BoardConsolePanel` got the optional `naming` prop (`FlashBoard` passes it when it has a fleet) and the fleet holds a row for the board (else the PATCH would 404). Capability by prop, the `DeployCell` pattern: `onSessionExpired` (401 drops to login) and `onSaved` (`fleet.refresh`). No callbacks in `ResultContext`, which stays data for the pure judgement. `ResultCard` takes a `naming` slot and decides nothing.
- **Draft (6).** Module-level component, keyed by device id (the panel re-renders at 1 Hz). Initialised from the row's name at mount and never resynced from props, so an SSE re-read cannot clobber typing; after a 200 it takes the server's normalised value.
- **Client check (7).** `boardName.ts::checkBoardName` mirrors the server (trim, blank to null, 64 code points, no `Cc`/`Cf`, not 12 hex) for a message before the round trip. The card sends the normalised value. Duplicates are not pre-checked: the 409 detail names the other board and is shown verbatim. JS `trim()` vs Python `strip()` differ at the edges; the server decides.
- **Readable 422s (8).** FastAPI's 422 `detail` is an array; `api.ts::detailOf` now joins each item's string `msg` (minus `Value error, `) with `; `. A string `detail` is unchanged byte for byte (DeployCell renders it verbatim); an array with no string `msg` still falls back to the raw body. Shared module, so the full frontend suite ran.
- **Messages and controls (9, 10).** `Saved. The fleet table shows “<name>”, with the id beneath it.` / `Name cleared. The fleet table shows the device id.`; errors are `ApiError.message` verbatim; a `role="status"` line (a discrete event, unlike the 1 Hz card). Button reads `Saving…` and is disabled while in flight, with a ref guard against a double submit. No `maxLength` attribute, no logging of the name.
- **T2 on the dev stack, real API + DB, Chromium with a fake `navigator.serial`.** Contract: the real 422 body (65 chars) is the `api.test.ts` fixture and reads `a name is at most 64 characters`; the 409 reads `name already used by device 0000000fe602: Hen House`. Browser: C form on the success card (empty input); D `  coop door  ` + Enter sent one PATCH `{"name":"coop door"}`, DB `coop door`; E fleet row showed `coop door` over the id with no reload; F `hen house` showed the 409 verbatim, DB unchanged; G a MAC-shaped name and H 65 characters gave client messages and no PATCH; I clearing sent `{"name":null}`, DB NULL, table back to the id; J no PATCH carried `group_id`.
- **Rejected:** a group picker with no list endpoint; a client duplicate pre-check; `maxLength` on the input; callbacks in `ResultContext`.
- **Proposed follow-ups, not filed:** rename from the fleet table (`NameBoard` takes `deviceId`, `currentName` and two callbacks, so it can be reused); a group picker after a read-only `GET /v1/groups`.

Supersedes nothing.

---

## 2026-10-04 — A board's name and group are set with one PATCH; "tag" is the existing group, not a new column (R2b-be-1)

**Decided: `PATCH /v1/devices/{id}` takes `{name?, group_id?}`, admin-only, merge-patch. Backend only; no migration, no spec edit.**

- **"Tag" is `devices.group_id`.** `spec/flows.md` treats group and tag as one concept and `spec/standards.md` says naming must not grow into a second, weaker grouping. A free-text column would need a migration.
- **Merge-patch.** Absent key: untouched. Explicit `null`: cleared. `{}` is a 200 no-op that writes and emits nothing. `extra="forbid"` so a typo is a 422.
- **Name rules (422).** Stripped; blank becomes NULL; at most 64 chars after stripping; no `Cc`/`Cf` characters; not 12-hex (a MAC-shaped name on board B would read as board A's id).
- **Unique among live boards, case-insensitively, in the app (409).** No DB constraint (would need a migration). Excludes the device itself and decommissioned rows.
- **Unknown group: 404 `no such group`; unknown/decommissioned device: 404 `no such device`.** All checks run before any assignment, so a refused PATCH changes nothing.
- **Response is the updated `DeviceSummary`.** Emits `device.updated` on `ff_events` in the same transaction; the name is not in the envelope. Logs carry field names only.
- **T2 on the dev stack:** A-L all as expected (200 trimmed, round trip, 409 naming be101, 422 x2, atomic 404, 404, 401, null clears, log lines list field names, 0 log hits for the name).
- **Rejected:** a `tag` column; a DB unique index on name; group CRUD (V3); the name in the event envelope.
- **Gotchas:** a field-level `max_length` counts unstripped text, so length is checked in the validator after stripping; `updated_at` (server-side onupdate) is expired after flush and raises `MissingGreenlet` if read in async, so it stays out of the summary and logs; `emit()` must precede `commit()` so a rolled-back write emits nothing.
- **Proposed follow-up, not filed:** a read-only `GET /v1/groups` before a group picker can exist in the UI.
- **Proposed follow-up, not filed:** a read-only `GET /v1/groups` before R2b-fe-6 can offer a group picker.

Supersedes nothing.

---

## 2026-10-04 — The console panel also reads the server's view: Enrolled / On the fleet are marked from the device list against a snapshot taken when the flash starts (R2b-fe-5)

**Decided: `serverWatch.ts` judges `Dashboard`'s one `useFleet` against a baseline; `BoardConsole.tsx` merges it into the console's summary. Frontend only. Details: `docs/features/enrollment.md` → *Two-source watch*.**

- **Baseline, never the browser clock (D1).** Server times are one clock (models.py rule 3); comparing them to `Date.now()` would depend on the operator's skew. A `FleetBaseline` is `{enrolled_at, last_seen}` per lowercased id.
- **Two anchors, only one re-taken (D2).** The flash anchor is owned by `FlashBoard`: taken on every entry into `flashing` (token minted inside it), cleared on `idle`. **Gotcha:** pressing "Watch a board" after a drop must not re-take it; the board may already have re-enrolled and a re-taken baseline would never show it. The watch anchor (no flash here) is taken by the panel on the first watch, kept across re-watches, dropped by Clear. A fleet still loading at the anchor is captured on first arrival (accepted limit: it could already hold the new enrolment).
- **Rules (D3).** After a flash: Enrolled = `broker_provisioned_at` set and `enrolled_at` changed from the baseline (or no prior row); On the fleet = Enrolled, `online`, and `last_seen > enrolled_at`. No flash: a held credential is Enrolled; On the fleet also needs `last_seen` to move past the baseline. This closes the re-flash trap: a known board's stale row says `online: true` with the old `enrolled_at` until `/v1/enroll` runs. `online` is read as-is (`fleet.ts` rule 1).
- **Only Enrolled and On the fleet (D4).** Arrival stages are not used for Network up / Clock set (arrival filtering makes them a poor per-session signal).
- **Merged, not forked (D5).** `mergeServerView` adds `enroll` / `fleet`, re-applies the furthest-reached rule, clears `overdue` when the board is on the fleet, the milestone was passed, or the console stopped (its deadline clock froze with the port), and clears `fault` only on the fleet. The classifier and `summarizeConsole` are unchanged.
- **What shows (D6).** Once the console stopped after trying, the checklist stays with `— from the server` items, a `console-server-view` line, the console's error text unchanged (Check F matches on it) but muted once enrolled, and a success card even with zero console lines. Never a failure card from a lost port alone.
- **No unbounded wait (D7).** `SERVER_WAIT_MS` = enroll + fleet deadlines (90 s) after the console stopped, on the fleet's 1 Hz `now`: `console-server-overdue`. The stop time is set when `watch()` settles, not off `opening`, because a fast acquire failure flips `opening` true and false in one React batch (deviation from the plan's effect).
- **Status strip and bundle unchanged (D9).** The strip already shows the flashed board's server state (R2b-fe-1); an onboarding-console segment is not built and no task is filed.
- **Rejected:** comparing server times to the browser clock; a second EventSource or `useFleet`; arrival stages for Network up / Clock set; a status-strip segment.

Supersedes nothing.

---

## 2026-10-04 — Agent-learned networks belong in an NVS overlay keyed to the ff_cfg it extends; keep identity is a flash that mints no token; Improv needs an RX path the S3 console does not give (R2b-spec-3, findings)

**Found: findings only, nothing built, `spec/` untouched. Details and the QEMU transcripts: `docs/features/enrollment.md` → *spike findings (R2b-spec-3)*.**

- **Keep identity already works on the device.** An `ff_cfg` with no token never erases the credential (`ff_store_sync_token` returns on an empty token) and the flasher never writes `nvs`. Shown in QEMU: a tokenless `agent-qemu-recfg`, and the browser flasher's full write plan minus the token, both boot to `reusing the stored credential (no enrollment)` with no `ff-enroll` line, the used-token count and `enrolled_at` unchanged. Every flash re-enrols today only because `flash.ts` mints; `validateFfCfg` does not require a token.
- **When NVS is gone the board parks, once, and the server hears nothing** (no token means no stage reports). The console classifier already names the `carries no enrollment` line. The following `halted:` reason wrongly says the token "was refused for good"; a wording fix, not filed.
- **Keep identity is offered only for a live row on this server with an unchanged layout.** `ab-4m-arduino-v1`'s `nvs` is 0x5000 not 0x6000 and its `otadata` sits inside `ab-4m-v1`'s `nvs`. The `api_base` must be the one the credential carries (the agent only warns). A stale `ff_txn` record is discarded without an outcome, exactly as on a re-enrol. After a whole-chip erase only a new token helps.
- **A list the agent edits goes in NVS, not `ff_cfg`.** `ff_cfg` is one sector: an erase+write cut by power leaves a board that parks with no server address, USB-only recovery; and the flasher rewrites it on every flash. The overlay lives in its own namespace (not `ff`, which the credential eraser owns), tagged with the `ff_cfg` header CRC it was learned on: a different CRC at boot discards it, an OTA never moves it. Effective list = overlay (newest first) then `ff_cfg`, deduplicated by SSID, cut at 4; `known_networks` counts it.
- **Improv over serial needs no protocol change** but needs an input path: IDF v5.5's default S3 secondary console (USB-Serial-JTAG) is output-only, so the native-USB bench board needs a console change (`sdkconfig.defaults*`, CRITICAL) or an app-installed reader. Try a network before persisting it, fall back on failure, refuse during an open OTA transaction. The dashboard must share `SerialConsole`'s one reader.
- *Rejected:* the agent rewriting `ff_cfg`; the overlay inside `ff`; copying `ssid`/`psk` to NVS at enrolment with `dn/cmd set_cfg` (a second source of truth, and passphrases through the server); a server-issued "re-flash token" that re-issues the same broker credential (new endpoint, protected spec, a credential oracle).
- **Follow-ups proposed, not filed:** frontend "Re-flash, keep identity"; spec for the overlay contract (CRITICAL); agent overlay reader (after R2b-fw-1); agent Improv; dashboard Improv client; agent wording and two stale comments. Proposed spec wording for `spec/flows.md` and `spec/open-questions.md` is prose, to apply after R2b-spec-1 Patch A.

Answers the *Still open* items of the 2026-10-04 R2b-spec-1, Flow 3 and Flow-edits entries, and the pre-flight card's "keep identity is not offered". Supersedes nothing.

---

## 2026-10-04 — The update result card replaces the one-line verdict for a finished transaction; before and after are two rows, no arrows; failures get one cause and one next action from the board's own words (R2b-fe-10)

**Decided: `deployResult.ts` judges a terminal `DeploySummary`, `DeployResultCard.tsx` renders it inside the Deploy cell. Details: `docs/features/dashboard.md`.**

- **Placement (D1).** Inside the Deploy cell, in place of `LiveState`, while `is_terminal` (the server's flag) and no pre-check card is open; the timeline stays below. Never two cards: Deploy hides the result and the compact line stands in, Cancel restores it. It lasts while the newest transaction is terminal; a new send replaces it. Rejected: a card below the table for the strip's board (far from the cell); a server-side `last_outcome` (already rejected by R2-fe-1).
- **Verdicts (D3).** `good` only while the announce equals the confirmed artifact, else `confirmed by the board` and the drift line, never "running". `failed before reboot` only when the steps are known and none reached rebooting, otherwise plain `failed`. Unknown terminal states still get a card. `deployOutcome` and `DEPLOY_OUTCOMES` are unchanged, so the status strip is unchanged.
- **Reason and next (D4).** The failure lookup is keyed by the detail up to the first colon (`Object.hasOwn`, since the key is device-controlled), and the board's exact words are always shown in `Board says`. The rollback reason names both causes and says the board does not say which; its next action is "Do not send X again as it is", never "send again" (spec).
- **No arrows (D5).** R2-fe-1's tests forbid `→` beside a verdict, so Before and After are separate rows. No `role`/`aria-live`: the cell re-renders every second.
- **Versions (D6).** `Dashboard` computes `describeVersions` once and passes it down; the standalone `FleetView` has none, so no `UI / API` row there.
- **One drift sentence (D7).** `deploy.ts::driftText`, used by the compact line and the card.
- **Out of scope.** Who sent it (`R2b-be-4`, no data; it adds a `Sent by` row); the Send again button (`R2b-fe-11`); the failed boot's crash reason and last milestone (the agent does not report them).

Supersedes nothing; refines R2-fe-1's placement (the verdict word now sits in the card).

---

## 2026-10-04 — A reboot during watch is counted as a restart with a reason; reached milestones are shown as lost, not silently reset (R2b-fe-4)

**Decided: `summarizeConsole` returns `lastReset`, `restarts` and `retracted`; the panel, result card and bundle show them. Details: `docs/features/enrollment.md`.**

- **Restarts, not boots.** A restart is an uncommanded boot boundary after the first boot seen, so the 2026-09-11 fixture (3 boots) is "Rebooted 2×". The reboot-loop banner's old "3 times so far" became "rebooted 2×: brownout" so the screen never shows both numbers; `rebootLoop.boots` and the bundle's "3 boots without reaching the fleet" are unchanged.
- **Reason precedence:** download mode (`boot:` field) > clue from the boot that ended (BOD line, or a panic line; brownout wins) > banner name > unknown. The agent's "previous boot ended in a BROWNOUT" line refines the current boot.
- **`SW_RESET` after a BOD line is a brownout.** `CONFIG_ESP_BROWNOUT_USE_INTR=y` makes the ISR restart the chip, so the real agent prints `rst:0x3 (SW_RESET)`. `RTCWDT_BROWN_OUT_RESET` is tested before `WDT`.
- **`POWER_GLITCH_RESET` and kin are not claimed as brownout.** They are `unknown`, shown as `other (ROM_NAME)`.
- **Commanded resets neither count nor retract.** This panel's own EN pulse empties the lost set.
- **Stage named only when all restarts agree**; mixed reasons are grouped with counts.
- **Rejected:** counting boots ("rebooted 3×" for 2 restarts); a status-strip segment (R2b-fe-5).

Supersedes nothing; refines S0-fe-4's loop banner wording.

---

## 2026-10-04 — The update timeline reads the current transaction's steps off `GET /v1/devices`; stall text states the board's own deadlines and never ends a deploy (R2b-fe-9)

**Decided: `DeploySummary` carries `steps` and `confirm_timeout_s`; `deployTimeline.ts` turns them into six milestones, elapsed seconds, a deadline and a stall sentence. Details: `docs/features/dashboard.md`.**

- **Steps ride on `DeploySummary`**, not a new endpoint: every row of the newest transaction, `{state, at}` only (no detail, so no sha256), oldest first by `at, id`, capped at the newest 32 (`deploys.MAX_DEPLOY_STEPS`), never empty (a NULL `cmd_id` row is a transaction of one). `confirm_timeout_s` is `Settings.confirm_timeout_s`, the pre-check's number. SQL in `deploys.py`, no migration.
- **Milestones are the spec's six**, raw states mapped by a lookup. A later milestone implies earlier ones ("not reported", never pending). `rolling_back` and unknown states map to none.
- **Stall text states only the board's rules**: the 60-80 s silent-download give-up (R2-fw-5) and the confirm window anchored on `rebooting`. `requested` online ≥ 30 s and the 60 s report slack after the window are display thresholds, not policy. `awaiting_safe_window` gets no deadline and no stall, ever. Nothing changes the state label, verdict or styling; `terminal` is the server's `is_terminal`.
- **Spec PROPOSAL (not applied):** Flow 2 step 4's "no data for 60 s; the board gives up at 80 s" implies the dashboard sees data flow. The agent publishes `downloading` once, so reword to "no word from the board for N; it gives up on its own 60 to 80 s after data stops".
- **Rejected:** a `/v1/devices/{id}/deploys` endpoint (a second refresh engine); a progress bar from `pct` (a transition log); a server-side `stalled` flag (the server expires nothing, `deploys.py` rule 1).

Supersedes nothing.

---

## 2026-10-04 — The Deploy button opens the pre-check card; Send is a second click; refusals offer no Send; `override` is sent only when non-empty (R2b-fe-8)

**Decided: Deploy runs `POST /deploy/precheck` and opens `PrecheckCard.tsx`; only the card's Send reaches `POST /deploy`. Details: `docs/features/dashboard.md`.**

- **Refusals offer no Send**, only Cancel, and say "Refusals cannot be overridden."
- **Non-gating warnings** (`never_connected`, `offline`, `sleepy`) are overridden by the Send click, whose label becomes "Send anyway". **Gating ones** (`needs_override`, R2b-be-7) also need a per-code tick.
- **`override` only when non-empty**, and only codes the pre-check raised as gating and the operator ticked. Today's `DeployRequest` forbids extra keys, so `override: []` would be a 422.
- **A stale pre-check answer is ignored** (sequence ref): a version change, Cancel or a newer Deploy click discards the card.
- **Server sentences verbatim**, with `Refused:` / `Warning:` as a separate label element.
- Flow 2 step 3 "one button" is the card's Send; no spec change.

Supersedes nothing.

---

## 2026-10-04 — A merged full-flash image is refused at upload with a 422, from two signatures; the pre-check never sees one (R2b-be-3)

**Decided: `POST /v1/artifact` answers 422 with one plain sentence when the body is a merged full-flash image. Details: `docs/features/ota-deploy.md`.**

- **At upload, before the store and the DB.** A deploy never reads bytes (R1-be-3), and a verdict per artifact would need a migration. With upload refusing, no merged label can exist.
- **Code and sentence in `deploy_precheck.py`** (`MERGED_BINARY`, `merged_binary()`), not in `refusals()`, which has no bytes and is unchanged.
- **Narrow and negative.** Either signature means merged: `0xFF` x 4 KiB then `0xE9` at 0x1000, or a partition table at 0x8000. Unknown files are still accepted.
- **Size.** An oversize merged file hits the existing 413 first; that sentence now names the merged trap and keeps both numbers.
- **Status 422**, `detail` is a plain string the form already shows verbatim; no frontend change.
- **Gaps.** Artifacts uploaded before this are not re-checked; `merge_bin --target-offset 0x1000` files are not recognised.

Supersedes, from the R2b-be-2 entry, `merged_binary` (R2b-be-3 adds it to this module) as a pre-check refusal: the code and sentence are in `deploy_precheck.py`, the check is at upload. Refines R1-be-1's "bytes are opaque" and R2b-fe-7's "the server stays opaque to bytes": one negative check, never format validation.

---

## 2026-10-04 — R2-spec-1 is amended, not applied as filed: `rollback_capable: false` is a gating warning with a per-code override, `flash_size` becomes `flash_chip_size`, and the spec defines `false` by its meaning (R2b-spec-2, proposed)

**Decided: amend the 2026-10-03 R2-spec-1 proposal, then apply it. Not "apply as filed",
not "drop". This entry covers the amended PROPOSAL only. Nothing is built: no agent,
server, frontend, migration, simulator or test change, and `spec/` is untouched; the owner
applies it in a separate `spec:` commit.** The amended text and the paste-ready patches are
in `docs/features/board-profiles.md` → *Step 1 wire proposal*.

- **Why not drop.** `spec/flows.md` Flow 2 step 2 (commit `2390029`) already requires a
  warning, with an explicit override, on `rollback_capable: false`; dropping the field
  leaves that requirement with no data source. And `partition_table_sha256` is the only
  check that catches two different tables that both announce `ab-4m-v1` (R3 makers bring
  their own `partitions.csv`).
- **Why not as filed: three conflicts.** (1) Policy: the filed server semantics make
  `false` a deploy 409 with no override; *Flow edits reviewed* (2026-10-04) and
  `spec/flows.md` made it a warning with an explicit override (bench racks must send).
  (2) Mechanism in a near-frozen spec: paste-ready (b) defined `false` as "stayed `NEW`",
  an unbenched signal. (3) Name collision: `flash_size` is already the bundle manifest's
  image-header string (`"4MB"`, `firmware/manifest.py`), the very claim the announce field
  must not be.
- **A1: `rollback_capable: false` is a gating warning, code `rollback_incapable`.**
  `POST /v1/devices/{id}/deploy` answers 409 with the same sentence unless the body carries
  `override: ["rollback_incapable"]`. Shape: `DeployRequest.override:
  list[Literal["rollback_incapable"]] = []`, per code, never a blanket `force` (the
  docstring's "no `force`" stays true); an unknown code is a 422; listing a code that is not
  raised is a no-op; refusals are never overridable. `PrecheckFinding` gains
  `needs_override: bool = False` so the card knows which warnings gate; `never_connected`,
  `offline`, `sleepy` stay non-gating. The R3 library marker will be the second gating code.
- **`null` / absent never warns and never refuses.** That is every board before its first
  OTA; a warning there would fire on every board and teach the operator to ignore warnings.
- **A2: the announce field is `flash_chip_size`** (integer bytes), in field 3, paste-ready
  (a) and (b), and the size budget.
- **A3: the spec defines `false` by meaning only:** the board booted an OTA-written image
  that its bootloader never put into `PENDING_VERIFY`. The `NEW`-at-`target_addr`
  mechanism stays in the feature doc. The agent emits `false` only after `R2b-test-5`
  benches it; until then it emits `true` or nothing, and the warning is exercised through
  the simulator.
- **A4: application order. Patch A (paste-ready (b), (c), (d)) any time after the owner
  accepts; Patch B ((a), the three new keys in the `up/announce` example) only in
  `R2b-fw-2`'s commit.** `test_the_firmware_builds_exactly_the_spec_keys` requires
  `ff_identity.c` to emit every key in that example, so Patch B first turns `just test` red.
  `"ota_slot_size": 1966080` and `"partition_layout": "ab-4m-v1"` stay byte-for-byte. Patch
  B coexists with R2b-spec-1's Patch B; whichever lands second rebases (≈ 590 B combined,
  under esp-mqtt's 1024 B).
- **Kept as filed:** three flat optional fields, `proto` 1, the geometry-only fingerprint
  rule and `ab-4m-v1` = `1fa67e6b...59ed` (re-computed 2026-10-04, matches), a fingerprint
  mismatch stays a refusal (now named `partition_table_mismatch`), enroll stores and never
  rejects, no fallback for the chip size, `ota_slots` / `bootloader_sha256` deferred.
- **Dependents filed and blocked on the owner:** `R2b-fw-2`, `R2b-be-6`, `R2b-be-7`
  (marked in `TODO.md`); `R2b-test-5` is hardware-gated. Until they land, the pre-check
  keeps skipping the `rollback_capable` warning.

Supersedes, from the 2026-10-03 R2-spec-1 entry, the `rollback_capable: false` half of
*Refuse-and-flag* (a 409 becomes a gating warning with an override) and the field name
`flash_size`. The rest of that entry stands.

---

## 2026-10-04 — The deploy pre-check is a side-effect-free twin of the deploy: same body, every reason at once, one module owns every sentence; warnings never block a send (R2b-be-2)

**Decided: `POST /v1/devices/{id}/deploy/precheck` takes `DeployRequest` and answers 200 with
`DeployPrecheck`. Details: `docs/features/ota-deploy.md`.**

- **Refusals travel in the 200 body**, so the card shows all of them. Only a bad version
  (400) and an unknown or decommissioned device (404) keep a status.
- **`no_artifact_for_target` is a refusal here** and stays a 404 on the deploy.
- **One source: `fleetforge/deploy_precheck.py`** (pure). Not `deploycheck.py`, which is the
  unrelated release gate. `/deploy` raises the first refusal, same statuses and `detail`.
- **Warnings** `never_connected` / `offline` / `sleepy`, computed from the router's `online`.
  They never block `/deploy`; no override field yet, since that belongs to gating warnings.
- **Not yet:** weak RSSI (not stored, R4); `rollback_capable: false` (R2b-spec-2);
  `merged_binary` (R2b-be-3 adds it to this module); R3 library marker; URL-configuration
  readiness (`/v1/readyz`).
- **`confirm_timeout_s` is in the body** for the card's "rolls back on its own" line.

Supersedes nothing.

---

## 2026-10-04 — Upload is a dashboard form over the raw-body endpoint; the header pre-fills target and version and a chip mismatch is refused in the browser; nginx takes 4m on `/v1/artifact` only (R2b-fe-7)

**Decided: `UploadBuild.tsx` posts the file itself to `POST /v1/artifact` and the retired
runbook is deleted. Details: `docs/features/dashboard.md`.**

- **Raw body, not multipart.** The server has no form parser; `FormData` would store the
  boundary lines as "firmware". `api.uploadArtifact` sends the `File` with
  `application/octet-stream`; only target/version/layout ride in the URL.
- **Target is a select, not free text.** A typo'd target is accepted and then matches no
  board, so the build never appears in any Deploy list. Options: fleet chips plus the agent's
  four targets.
- **Layout is a select**, defaulting to the one layout the chip's boards report: deploy
  compatibility is layout equality, so an Arduino board would 409 against an `ab-4m-v1`
  label.
- **The mismatch refusal is UI only.** A file whose header chip differs from the chosen
  target is refused in the browser; the server stays opaque to bytes. The header read is
  advisory, and refusing merged binaries stays R2b-be-3.
- **No client-side version regex.** The server's `VERSION_PATTERN` sentence is shown verbatim.
- **nginx's default body limit is 1m** and would have 413'd every image over 1 MiB in prod
  (the Vite dev proxy hides it). `location = /v1/artifact` takes 4m; the API stays the size
  authority (1966080). The general `/v1/` block is unchanged;
  `tests/test_frontend_nginx.py` guards both.
- **Runbook deleted;** `rollback-test.md` now uses the form.

Supersedes nothing.


---

## 2026-10-04 — The onboarding result card names one cause and holds the one action; flash write failures say cable, port, baud first and name the chip only on repeat (R2b-fe-3)

**Decided: one result card (`ResultCard.tsx`) renders a pure `OnboardingResult`
(`onboardingResult.ts`) or a `FlashFailure` (`flashFailure.ts`) and decides nothing. The
console panel renders the success/failure card; `FlashBoard` renders the flash-failed card
for throws from `flasher.write` only, and hides the console card while it shows. Never two
cards.** Details: `docs/features/enrollment.md` → *Operator-flow additions*.

- **When the console card shows.** Success: the console reached `fleet`. Failure: not, and
  the fault has a remedy, a milestone is overdue, or the board is looping. Otherwise none:
  while the board progresses the checklist is the view, so a transient disconnect that
  recovers never flashes a failure card. None with no events.
- **One cause, set at the classifier.** `Cause` (power, wifi, clock, server, broker, token,
  download-mode, firmware) rides on every specific hint and on `fault`; generic hints carry
  none. The card's cause: `fault.cause`, else power for a loop with no fault, else
  `MILESTONE_CAUSE[waitingFor]`, else "Stopped before …".
- **The card says the headline and one next action, not the long hint.** The watch
  paragraphs keep their text and test ids.
- **The one action moved into the card.** `remedyAction(fault.remedy ?? overdue.remedy)`,
  same precedence as before; text `CAUSE_NEXT` when no button renders. This refines
  S0-fe-6's placement (the button left the fault/overdue paragraphs); the one-button rule
  and its test are unchanged.
- **One copy click.** A failure card holds "Copy diagnostic bundle"; the toolbar's is hidden
  meanwhile. The flash-failed card offers none (the console has nothing yet).
- **The success card absorbs "This board enrolled and is on the fleet"** and its test id.
- **Sources, console first.** Device id: `ff-id` (or `mqtt connected as`) → prediction.
  Firmware: agent banner → device row → manifest "(written)". Layout: device row → build.
  Link: console SSID → form SSID, console ip. Clock: "NTP (server)" / "Kept across the
  reset (no answer from server)" / "Not set (…)". Facts are this boot only.
- **Flash write failures.** Four kinds (lost, no-answer, rejected, other) plus the part and
  address. Attempt 1: another cable or port first; "retry at 115200" above that baud.
  Repeat above 115200: "Retry at 115200" (sets the baud and re-flashes in the same click,
  no await before `reflash`). Repeat at 115200: the only mention of "flash chip". The
  count is per board, in memory (`useRef`), never storage; a good write deletes it.
- **Write failures keep esptool's own words in `error`.** Deviation from the plan, which
  kept `explainFlashError` there: it maps `No serial data received` to "Hold BOOT while
  plugging it in", which is right at connect and wrong mid-write, and the card shows
  `error` as its details line. Connect and pre-write failures are translated as before.
- **Labels** "Try the flash again" and "Retry at 115200" collide with no existing button
  name regex.
- **Out of scope:** the strip's onboarding segment and server-truth success when the
  console lost the port (R2b-fe-5); boot count/reset reason rows (R2b-fe-4); naming from
  the card (R2b-fe-6); known-networks rows (R2b-fe-12/13).

Supersedes nothing.

---

## 2026-10-04 — A board knows up to four Wi-Fi networks, joins the first it can see, and announces only the one it is on (R2b-spec-1, proposed)

**Decided: `ff_cfg` keeps its top-level `ssid` / `psk` as the first network and gains an
optional `nets` array for up to three more, in priority order (at most 4 networks in all);
the board joins the first known network its scan sees, by fixed priority rather than signal
strength, and stays on it until the link drops; `up/announce` gains two
optional flat fields, `ssid` and `known_networks`. This entry covers the PROPOSAL only.
Nothing is built: no agent, server, frontend, migration, simulator or test change, and
`spec/` is untouched.** The full text and the two paste-ready patches are in
`docs/features/enrollment.md` → *Known networks: wire proposal (R2b-spec-1)*.

- **Format "A": top-level first, `nets` for the rest.** An old blob is a list of one with no
  branching, and an old agent (whose reader ignores unknown keys) joins network 1. Rejected:
  `nets` holding every network with the top level mirroring `nets[0]` (two sources of truth),
  `nets` with no top level (old readers idle), and a version bump.
- **At most 4 networks in all.** A worst-case escaped entry is about 340 B, so 4 cost about
  1.4 KB; with a ~600 B worst-case baseline about 2 KB of the 4080-byte payload stays free
  for a possible CA root. Writers (`ff_cfg.py validate`, `ffcfg.ts validateFfCfg`) refuse
  more; a reader that finds more uses what fits, warns, and never refuses to boot.
- **Fixed-priority selection, no roaming while associated.** List order wins, the strongest
  AP within one SSID, fall-through past a visible network that gives no address, hidden SSIDs
  tried directly, and one network connects directly with no scan (QEMU run unchanged).
  Rejected: strongest-in-range, which flaps at similar RSSI and makes "on: shed"
  unpredictable.
- **No known network in range:** a console line per attempt cycle, keeps trying on the
  1 s → 30 s backoff, never reboots or opens an AP. **The server cannot see "no network in
  range"**: with no link it sees only an offline board. Only the result card, which reads the
  console, can name it; the Fleet row says "offline, last on: shed". This corrects Flow 3
  step 5 and `R2b-fe-13`.
- **Announce `ssid` and `known_networks`.** `ssid` is the network this broker session runs
  over; `known_networks` is how many the board will try. `known_networks` was added beyond
  the TODO line because "knows 2 networks" has no other source: the server never sees
  `ff_cfg`. Both `null` on ethernet, absent on older agents (absent = `null` = not
  reported). Never the passphrase or the other SSIDs. Enroll stores them, never rejects.
- **The `ff_cfg` header version stays 1.** `ff_cfg_load()` refuses any other version, so a
  bump would idle every fielded board.
- **Downgrade is caught by the R2 rollback.** A pre-list image on a board in range of only
  `nets[...]` never confirms and rolls back unattended. The R3 library must read `nets`, or
  every OTA to a maker image works only within range of network 1.
- **Application order: Patch A (prose) any time after acceptance, Patch B (the two
  announce-example lines) in the R2b-fw-1 commit.** `test_the_firmware_builds_exactly_the_spec_keys`
  requires `ff_identity.c` to emit every key in that example, so B before fw-1 turns
  `just test` red.
- **Dependents are blocked until the owner accepts:** `R2b-fw-1`, `R2b-fe-12`, `R2b-be-5`
  (marked in `TODO.md`), so `/implement-all` does not build against an unapproved spec.
- **Redaction gotcha.** `ff_cfg.py describe()`, `ff_cfg_log()`, the diagnostic bundle and
  `ffcfg.ts` errors redact only a top-level `psk`; each must redact `nets[].psk` before
  `nets` ships.
- **Still open:** NVS vs `ff_cfg` once the agent can write the list (R2b-spec-3).

Answers the *Open* items of the 2026-10-04 Flow 3 entry (count, selection rule, `ff_cfg`
format, no-network report); supersedes nothing.


---

## 2026-10-04 — The pre-flight card is shown on detect, never blocks, and the known-board button says re-enrol (R2b-fe-2)

**Decided: once a chip is detected, a card between identify and configure says whether the
predicted id is a new board or already on the fleet. It never disables Flash, and for a known
board the button reads "Re-flash and re-enrol this board".**

- **No modal, no confirm box.** The spec asks that the operator be told; the card is above the
  button and the label carries the consequence into the click.
- **Non-blocking.** While the fleet is loading or unreadable the card says it could not check
  and states the consequence conditionally. Blocking would let a dead API stop flashing.
- **One fleet.** `Dashboard` passes its `useFleet` result to `FlashBoard` as a prop; without the
  prop there is no card. Never open a second `useFleet` there.
- **Hidden after a successful flash**, when the Flashed section takes over.
- **"Re-flash, keep identity" is not offered**: a requirement without a mechanism (R2b-spec-3).
- **The console's re-flash recovery button (S0-fe-6) shows no card.** It is a one-click flash
  of the board just flashed in this tab; a stop there would undo S0-fe-6.
- **Ids compare case-insensitively.** A retired board is absent from `GET /v1/devices`, so it
  reads as "not on the fleet", and the copy claims no more.
- Supersedes nothing.


---

## 2026-10-04 — The status strip's board is the one last picked, deployed to or flashed; "differ" compares version and commit (R2b-fe-1)

**Decided: the strip shows the board the operator last picked in the Fleet table, deployed
to, or detected/flashed; with nothing picked and one board, that board. "UI and API differ"
is true when both versions are known and unequal, or both commits are known and their
8-char prefixes are unequal.**

- **Why a selection at all.** Nothing selected a board before; "after a deploy, one glance"
  needs the deployed row to become the strip's board.
- **Why both version and commit.** In prod the two images share a commit, so a stale bundle
  at the same version is still caught. Unknown on either side means no claim.
- **Scope cut.** The onboarding console state is not in the strip yet (R2b-fe-3/fe-5); the
  state is presence, deploy state/verdict (`deployOutcome`), or the arrival stage.
- **One fleet hook.** `Dashboard` owns the only `useFleet` (one SSE slot); `FleetTable`
  takes it as a prop. `/v1/healthz` is polled every 60 s and never touches the session.
- Supersedes nothing.


---

## 2026-10-04 — Prod's flasher should serve the repo's agent; the 0.3.x baseline is not kept as the default (S0-infra-10)

**Decided: publish 0.4.5 as the flasher default. A lag check now exists and `just build`
warns on it. The prod publish itself is owed to the operator.**

- **The baseline is already gone.** The S0-bug-1 bench flash re-enrolled `94a990dd09a4` from
  prod's flasher, so it runs 0.3.2, not 0.3.1. The "STOP, ends the 0.3.1 baseline" boxes in
  `docs/runbooks/serial-console-bench.md` (Check E/F) are moot; this refines them.
- **Publishing loses nothing.** The displaced digests go onto `superseded`. Rollback is
  `just agent-rollback <target> <digest>` against prod's env. Prod today:
  esp32 0.2.0 `8b35fe50...c652`, esp32c3 0.2.0 `fa6f9b41...fa05`, esp32c6 0.2.0
  `a1fdf71c...cf04`, esp32s3 0.3.2 `30df6a68f0cd56193a20f2499979030e7994a3363c0559185747d255313f1533`
  (was 0.2.0 `3bfdf57f...55f0`).
- **Open R2/R2b tasks assume >= 0.4.x** on new boards, and a 0.3.x board brings back the
  transition gap (first deploy parks at `rebooting`, rollback-test.md). A re-flash from the
  flash page today would install 0.3.2, without R2-fw-1..6.
- **The check runs on the dev box** against prod's GCS index (`just agent-check-prod`,
  read-only), because prod's container has no checkout. `just build` runs it with
  `--warn-only`. The root `/release` skill is in the root repo: change proposed, not made.
- **The agent did not publish to prod.** A blanket `/implement-all` run is not a go-ahead
  for a write that changes the firmware every new board gets.
- Supersedes nothing.


---

## 2026-10-04 — A later milestone implies the earlier ones; a clock can survive a reset (S0-bug-1)

**Decided: the console's `waitingFor` is the milestone after the furthest one reached, not
the first unreached one. Earlier milestones that never logged are reported as `skipped`.
`reached` stays "seen this boot" (S0-fe-4's stale-tick rule stands; nothing superseded).**

- **Why.** On the 2026-10-04 bench the SNTP wait timed out, yet enrolment over https and
  MQTT worked: with `MBEDTLS_HAVE_TIME_DATE=y` a passing TLS handshake proves the clock, and
  the S3's RTC survives the native-USB `hard_reset`. The board was on the fleet from 14:20:08
  to 14:24:58 while the panel said "waiting for Clock set" and never showed the banner.
- **Same rule covers** a reboot with a stored credential (no `enroll 200` line): `enroll` is
  `skipped`, not waited on.
- **Hint softened.** `sntp: no answer` with a clock year >= 2024 (`FF_TIME_SANE_YEAR`) is a
  generic hint; with 1970 it stays specific, now "will fail until the clock is set".
- **The on-fleet banner** keys on `reached` including `fleet`.
- **Cause B** (board silent from ~14:24:13, never back) is unresolved: power removed vs
  firmware wedge needs an operator power-cycle run, see TODO S0-bug-1.
- **Side finding:** prod serves stale agent bundles (0.3.2 / 0.2.0 vs repo 0.4.5), filed as
  S0-infra-10. Publishing to prod was not done.


---

## 2026-10-04 — Flow 3: a board knows several Wi-Fi networks; Improv later (corrects the Wi-Fi wording of the entries below)

**Decided: `spec/flows.md` gets Flow 3, "Change the network a board uses". v1 is a list of
known networks added during onboarding, so a maker can move a board between home and the
shed with no new flash. Improv over serial, then over Bluetooth, come after and are not
specified. Spec only: nothing is built and no task is filed.**

- **Corrects the two earlier entries of this date.** They said Wi-Fi credentials are
  "remembered in this browser". `frontend/src/FlashBoard.tsx` already forbids `localStorage`
  and `sessionStorage` for that feature, because the passphrase and the enrolment token are
  credentials. The rule stands. The passphrase field is marked for the browser's own
  password manager instead, so the operator types each one once and the app stores nothing.
- **Why a list first.** The common maker move is between networks the maker already knows.
  A list needs no radio stack and no write path in the agent: the flasher already writes
  `ff_cfg`. A network not on the list still means a re-flash until Improv lands.
- **Supersedes** the Flow 1 line "a Wi-Fi change means re-flash" for networks on the list.
- **R3 consequence.** A maker's own firmware must read the same list and later carry the
  Improv handler, or an OTA to it strands the board on its current network. Not yet in
  `docs/releases.md` or `docs/features/ota-library.md`.
- **Bluetooth needs no phone app** on Chrome for Android or desktop; iPhone Safari lacks Web
  Bluetooth, so an iPhone needs an app or another browser. Marcus's customer-joins-their-own-
  Wi-Fi case is not served until Bluetooth or a captive portal exists.
- **Open:** network count and selection rule; the `ff_cfg` format change (to be proposed to
  the protected `spec/device-protocol.md`); how a board reports "no known network in range";
  `ff_cfg` or NVS once the agent can write its networks.


## 2026-10-04 — Flow edits reviewed: refusals gated or softened, implementation moved out of spec (supersedes parts of the two entries below)

**Decided: the `spec/flows.md` additions of commit `624d218` stay, with six changes. This
supersedes the "send again is open" line of the updates entry and adds the decisions that
commit made without an entry. Spec and docs only; nothing is built.**

- **Kept, now decided.** Watch Web Serial and the server's enrolment together (a
  native-USB port reset must not read as "no board"); retract milestones on reboot and show
  the boot count and reset reason; a failed Result card carries one recovery action and a
  redacted diagnostic bundle; refuse a merged full-flash binary at pre-check; "send again"
  after a failure before reboot (safe: sends are deduplicated on the board).
- **R3 items are labelled R3.** The Arduino layout (`ab-4m-arduino-v1`) as the maker
  default and the "no library marker" check need the embeddable library, which ships after
  R2 (`design/decisions/ota-library-ships-after-safe-deploy.md`). Until R3 the default
  layout is the stock one and no marker check exists; the marker's encoding is open.
- **`rollback_capable: false` is a warning with an explicit override, not a refusal.** The
  field is unknown until a board's first OTA, so it cannot protect that OTA, and the `false`
  reading is unbenched (2026-10-03). A bench rack (Sarah, Siddharth) must be able to
  override.
- **Two claims weakened.** A verify failure no longer names a defective flash chip (a cable,
  hub or baud rate is likelier; the chip is named only on repeat). The crash reason after a
  rollback is best effort: the surviving slot may see the rollback reset, not the failed
  boot's, and RTC memory does not survive a power loss. Feasibility is unestablished.
- **The layout choice is made for the operator.** Step 3 shows it under "Advanced" only; Alex
  does not know what a partition layout is.
- **Implementation detail left the spec.** Offsets, reset-reason handling, the marker and the
  bundle matrix are in `docs/features/enrollment.md` and `ota-deploy.md` under *Operator-flow
  additions*. The status-strip example in Flow 1 is back to versions, board, firmware, state.
- **Still open:** how the library marker is encoded (R3); sleepy nodes and the confirm timer;
  how "re-flash, keep identity" keeps the credential without burning a token.


## 2026-10-04 — Firmware updates are one guided flow with one result; Alex first (flows.md Flow 2 operator view added)

**Decided: `spec/flows.md` Flow 2 gains an operator view of five steps — Pick, Pre-check,
Send, Watch, Result — above the existing 7-step transaction, which is unchanged and keeps its
step numbers. Same persona order as the onboarding decision of the same date: Alex primary,
Marcus second, Siddharth and Sarah via the same API resource, Elena deferred. Spec change
only: no frontend, API, agent or schema change is built and no task is filed.**

- **Why.** Today the pieces work (the `good` / `rolled back` verdict, deduplicated sends,
  auto-rollback) but nothing says what will change or what happened: the version dropdown
  and Deploy button never say whether the build fits the board, the result is a word on a
  table row, and the before/after versions sit in different places. The only way to get a
  build in is a shell script, against `spec/standards.md` → *dashboard*.
- **Pre-check refuses a mismatched layout before sending.** CUJ-1 already lists "a wrong
  flash layout is refused, not flashed" as a hard-fail trap; step 2 is where the operator
  meets it, in plain language.
- **The deployment is an API resource** carrying state, timeline, verdict, who and when.
  The dashboard renders it; CI and a HIL rack read the same thing; it is the audit record
  Marcus needs. Rings and provenance (V2) build on it.
- **No progress bar, kept.** The agent publishes `downloading` once; the timeline shows
  elapsed time and the stall rule, not a percentage.
- **Still one board at a time in v1.** Groups and bulk deploy stay V3; Elena's
  stage-in-air, apply-on-ground only requires that "staged" stay distinct from "apply".
- **Open, not decided:** sleepy battery nodes and the confirm timer (a node that wakes,
  reports and sleeps in seconds can be rolled back falsely), and a "send again" action after
  a failed update.
- Supersedes no earlier entry; extends the Flow 2 text in `spec/flows.md`.


## 2026-10-04 — First-board onboarding is one guided flow with one result; Alex first (flows.md Flow 1 rewritten)

**Decided: `spec/flows.md` Flow 1 is rewritten as six steps — Connect, Identify (with a
pre-flight card), Configure, Flash, Watch (milestone timeline), Result — with a status strip
that shows the UI version, API version, the board and its firmware, and the onboarding state
in one place. Persona order for this flow: Alex (hobbyist) primary; Marcus (OEM) second;
Siddharth (CI) and Sarah (HIL lab) served by the same API resource; Elena (swarm) deferred
except for showing the clock source. This entry records the spec change only: no frontend,
API, agent or schema change is built, and no task is filed yet.**

- **Why now.** A bench session on 2026-10-04 (`S0-test-1` / `S0-test-2`) had the operator
  hunting between the page footer (UI/API version), the Fleet table (firmware) and the flasher
  (port, progress) to answer one question, "is this board on the version I just deployed?",
  and a board waiting on "Clock set" gave no hint whether it was slow, retrying or failed.
  `spec/standards.md` → *Unaided onboarding* already forbids reading a raw log as a step; the
  flow did not yet say how.
- **Pre-flight before destructive.** A flash that re-enrols a board ends its identity and
  baseline (the 0.3.1 baseline on `94a990dd09a4` was given up this way). Step 2 must say what
  will change first and offer "re-flash, keep identity". The requirement is spec'd; the
  mechanism (keep the broker credential and `ff_cfg` without burning a token) is an open
  design question for `docs/features/enrollment.md`. Until it is built every flash still
  mints a fresh token.
- **Wi-Fi credentials are remembered in the browser only.** Never sent to or stored by the
  server. This removes the retype on every flash without widening what the server holds.
- **Onboarding is an API resource.** An *onboarding session*: `state`, milestones with
  timestamps, plain-language stall text, final result. The dashboard renders it; CI, a HIL
  rack and the post-v1 batch CLI read the same thing. Headless use needs no second design.
- **Persona order, and what it costs.** Alex first because the *Unaided onboarding*
  standard describes them. Marcus's pilot board uses the browser flow, and batch flashing
  stays post-v1. Elena's field gateway (local broker, artifact cache, time source) stays out
  of v1: the only obligation is that step 5 names the clock source and explains the 15 s
  SNTP stall.
- **Cost of step 5.** A useful milestone timeline needs the agent to report clock and link
  state more clearly than the log lines do today. That is an agent and protocol question;
  nothing is decided about the wire format here.
- **Left alone:** the old step numbers cited in `docs/features/dashboard.md` (step 7, naming,
  now the end of step 6) and `docs/features/infrastructure.md` (step 4, the flash and its
  partition) still resolve; the board-side exchange is unchanged and relabelled B1/B2.
  Supersedes no earlier entry; it replaces the Flow 1 text in `spec/flows.md`.

## 2026-10-03 — rollback_capable is measured, never claimed; the partition fingerprint hashes geometry, not labels (R2-spec-1, proposed)

**Decided: step 1 of board-profiles puts three additive fields on `up/announce`:
`rollback_capable` (`true | false | null`), `partition_table_sha256` and `flash_size`. This
entry covers the PROPOSAL only. Nothing is built: no agent, server, schema, migration,
simulator or test change, and `spec/` is untouched.** The full text, with worked values and
paste-ready spec edits, is in `docs/features/board-profiles.md` → *Step 1 wire proposal
(R2-spec-1)*. The three-way choice of design in that file stays a plan until step 1 is built.

- **`rollback_capable` is a reading, not a claim.** `true` = the board has booted an
  OTA-written image in `PENDING_VERIFY`. `false` = an OTA-written image runs at the
  transaction's target slot in `ESP_OTA_IMG_NEW`, so the bootloader did not transition it
  (the case `classify_txn()` drops today as "stale, discarded", leaving the deploy parked at
  `rebooting`). `null`/absent = not yet observed, which is every board before its first OTA.
  It is never derived from the app's `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`: that describes
  a build, not the bootloader in flash. Distinct from `up/hb` `boot_ok`, which is per-boot.
- **The limit, accepted.** A board cannot measure its bootloader before its first OTA, so
  the first OTA to a rollback-less board is unprotected and the field only says so
  afterwards. The `false` signal itself is unmeasured on a real rollback-less bootloader
  (the 6.2 migration note says the app marks itself valid, which would look like `VALID`),
  and must be benched before it goes on the wire.
- **The fingerprint hashes geometry.** SHA-256 over `{type}:{subtype}:{offset}:{size}\n`
  lines, decimal, sorted by offset, for every entry in the decoded table. Labels are out
  (behaviour-identical tables must match; the agent finds `ff_cfg` by subtype) and flags are
  out (the runtime `encrypted` flag is not the table's). Decoded, not the raw sector, so the
  server and tests recompute it from a CSV with no ESP-IDF tooling. Worked values:
  `ab-4m-v1` = `1fa67e6b...59ed` (from `agent/partitions.csv`), `ab-4m-arduino-v1` =
  `05528998...1fc4` (from the ADR's table, until R3 checks in a CSV).
- **Verified against ESP-IDF v5.5.5, and two planning assumptions were wrong.**
  `esp_bootloader_get_description()` exists but returns the **app's** compiled-in descriptor
  ("intended for use by the bootloader"), so it cannot detect a swapped bootloader: the
  Arduino-IDE-upload staleness of a persisted `rollback_capable` is an accepted, named gap.
  Bootloader / partition-table types `0x02`/`0x03` are ordinary table entries, not runtime
  pseudo-entries, so the fingerprint rule is simply "every entry". `flash_size` uses
  `esp_flash_get_physical_size()` and is omitted on failure, with no fallback to
  `esp_flash_get_size()`, which is the image header's claim.
- **Refuse-and-flag, not adopt-and-warn.** Step 1 has only the layout name, and adopting a
  measurement needs step 2's fingerprint-to-profile table. So a fingerprint mismatch and
  `rollback_capable: false` are each a deploy 409 that names what did not match. `null`,
  absent, or a layout with no known fingerprint is never refused. Per-fleet vs global
  `detected` profiles stays open for R3.
- **Enroll stores, never rejects.** The enroll body is the announce object plus `token`, so
  the new fields reach `POST /v1/enroll`. A malformed value is stored as null and logged:
  a 400 there comes after the token is read and would cost it.
- **Deferred, with reasons.** `ota_slots` / a full partition list (the fingerprint covers
  step 1; a nested list strains the flat, CBOR drop-in rule; additive later). And
  `bootloader_sha256`, the only pre-first-OTA attestation and the only sound invalidation
  key for a persisted `rollback_capable`: esptool rewrites the image header's flash
  parameters at write time, so the on-flash digest need not equal the bundle file's, and it
  needs a bench measurement first. Rejected outright: a compiled-in claim.
- **No follow-up tasks filed.** `/implement-all` would pick them up and build against an
  unapproved spec change. They wait in `docs/features/board-profiles.md` until the spec
  proposal is applied.
- **Spec proposal (not applied).** `spec/device-protocol.md` → `up/announce`: add
  `"flash_size": 4194304, "partition_table_sha256": "1fa67e6b...59ed",
  "rollback_capable": true` after `ota_slot_size` (keep `"ota_slot_size": 1966080` and
  `"partition_layout": "ab-4m-v1"` byte-for-byte, `tests/test_agent_partitions.py` matches
  them), a paragraph defining the three fields under the `partition_layout` paragraph, and a
  `partition_table_sha256` column on the *Partition layouts* table. `spec/open-questions.md`:
  delete *Bootloader attestation on the Arduino path* and refile *Attestation before the
  first OTA* (`bootloader_sha256`). Paste-ready text for all four is in the board-profiles
  section above.

---

## 2026-10-03 — a re-delivered stage for the update in progress is ignored, not failed (R2-fw-6)

**Decided: a `stage` whose `id` is the update this board is already carrying out is logged
and ignored. Nothing is published, parsed or started.** Agent 0.4.5. Fixes the
"Re-POST finding" of the R2-test-2 entry below, which stays as written. Proof status:
**proven in QEMU (esp32)**. A bench replay beyond a normal deploy is not needed: this is pure
command-seam logic with no radio or timing dependency. Transcripts:
`docs/features/ota-deploy.md` → *A re-delivered stage is ignored, not failed (R2-fw-6)*.

- **The rule. "Carrying out" means exactly one of two things.**
  1. *In flight:* `s_running` is true and its cmd_id equals the stage's id.
  2. *Staged and waiting:* `esp_ota_get_boot_partition() != esp_ota_get_running_partition()`
     and `ff_txn_load()` returns a record whose `cmd_id` is the stage's id.
     `apply: "on_command"` leaves the board in this state. Its server row is still
     non-terminal, so a re-POST is `reused: true` with the same id. Before the fix it got
     `staging, failed "an update is already staged and waits for a reboot"` against
     **its own** id, and the `confirmed` after the reboot was dropped. spec/device-protocol.md
     already says a device that has the transaction treats a repeat as a duplicate, so this
     is a conformance fix and the spec does not change.
- **A predicate in ff_ota, not a new return code.** `bool ff_ota_is_handling(const char *)`
  lives in ff_ota.c, the owner of "what is running". `ff_mqtt.c::on_stage()` calls it right
  after the id length check, before the artifact is parsed. That gives three things:
  `ff_ota_start()`'s contract stays as it was (`ESP_ERR_INVALID_STATE` = a *different* id is
  running); a re-delivery cannot produce a parse-time `failed` either; and nothing depends
  on a return code IDF v5.5.5 may lack. on_command()'s one-id dedupe is untouched.
- **Concurrency, with no mutex.** `static char s_running_cmd_id[64]` is written only in
  `ff_ota_start()`, immediately before `s_running = true`. It is never cleared, and
  `ota_task` never touches it. The predicate and `ff_ota_start()` both run on the esp-mqtt
  task, so the only variable shared across tasks is the existing `volatile bool s_running`,
  which ota_task moves true → false at `done:`. If that move lands between the predicate and
  `ff_ota_start()`, the stage simply runs again, which is the pre-existing behaviour for a
  finished cmd. When `s_running` is true for a different id the predicate returns false and
  never falls through to the staged check. Between finish() and `done:`, the record and the
  boot pointer belong to the running update.
- **Read-only.** No publish, no `fail()`, no `ff_txn_save`/`ff_txn_clear_if`, no otadata
  write. `ff_txn_load()` does clear a torn record, as it does at boot. A torn record's
  report is already lost, so that clear changes nothing.
- **Deliberately not a "seen in this boot" set.** A re-delivery of a cmd whose run already
  ENDED (failed, or reset mid-download) runs again. That is the recovery path that
  rollback-test.md (power-cut step 4) and ota-deploy.md D2 rely on, and QEMU S3 re-proves
  it. A seen-set would turn a lost report into a stuck deploy. The simulator's `seen` set
  is broader. That divergence is accepted: the simulator has no lost-publish path.
- **No re-publish of `staged` on an ignored re-delivery.** The server already has the row's
  last state, and the outcome after the reboot comes from the ff_txn record. An `apply`
  mismatch between the original and the re-POST is ignored too, because the repeat is a
  duplicate and not a new instruction.
- **Unchanged.** A different id while a download runs still gets `failed` / `another update
  is already in progress`. A different id while an image is staged still gets `an update is
  already staged and waits for a reboot`. Both are honest, because that cmd will not be
  carried out. After a reset both predicates are false.
- **Size.** esp32 app +288 B (1,018,304), esp32s3 +320 B (998,672).
- **Spec proposal (not applied).** `spec/device-protocol.md` → `dn/cmd`, after "A retried
  command reuses its `id`": "Deduplication covers the transaction the device is carrying
  out, not only the last command received: a `stage` whose `id` is the download in
  progress, or the image staged and waiting for a reboot, is ignored without a status,
  whatever arrived in between. A device that no longer has the transaction (e.g. after a
  reset mid-download) carries the repeat out."

---

## 2026-10-03 — a download that stops making progress fails after 60 s (R2-fw-5)

**Decided: `ff_ota.c::ota_task()` abandons a download whose image length has not grown for
`OTA_STALL_MS` (60 s, wall clock) and reports `failed` / `download stalled`; `s_running`
clears and the next `stage` runs.** Agent 0.4.4. Fixes D3 of the R2-test-2 entry below,
which stays as written. Proof status: **proven in QEMU (esp32), bench replay owed**
(`docs/runbooks/rollback-test.md` → *Marginal radio*). Transcripts:
`docs/features/ota-deploy.md` → *A stalled download fails (R2-fw-5)*.

- **Wall clock, not a read counter.** Equivalent to "K = 3 empty 20 s reads", but it keeps
  its meaning if `OTA_HTTP_TIMEOUT_MS` changes, and it also covers the wait for the first
  body byte (the clock starts at `downloading`, right after `esp_https_ota_begin()`).
  `_Static_assert(OTA_STALL_MS >= 2 * OTA_HTTP_TIMEOUT_MS)` so one slow read cannot trip it.
- **Granularity, measured (the plan said "normally ~60 s"; it is ~80 s).** The check runs
  each time `perform()` returns, every 20 s while stalled. The read in flight when the link
  goes silent returns its partial bytes only at its 20 s timeout, and that counts as
  progress. So the abort lands ≈ 80 s after the last byte (60 s at the earliest). QEMU:
  80.1 s and 80.2 s after the last progress line. Docs say "60-80 s"; the logged
  `no bytes for 60 s` counts from the last return that brought bytes.
- **Why 60 s and not longer.** On metal, TCP keepalive (`keep_alive_enable`, IDF 5 s / 5 s
  / 3) ends a socket whose radio is really gone in ~20 s (D4, `download failed`). The stall
  guard only fires for "far end alive but silent" (a wedged store or proxy, QEMU slirp),
  and the distinct detail tells the operator which one happened.
- **Failure shape = the `download failed` branch.** `esp_https_ota_abort()`, `fail()`,
  `goto done`. No `finish()`, no otadata write, no `ff_txn_save`, no
  `restore_boot_partition()`. The branch sits BEFORE `download failed` (after the break
  `err` is still IN_PROGRESS), and the bookkeeping runs before the loop's size-less
  `continue`. `detail` is exactly `download stalled`; seconds and bytes go to serial only;
  the URL is never logged.
- **Accepted behaviour change.** R2-test-2's D2 (90 s silent outage, then the link returns)
  used to resume to `staged`; it now ends `failed` / `download stalled`. This supersedes
  the D2 expectation. An outage shorter than the budget still resumes (QEMU S2, 30 s). The
  recovery is a re-deploy.
- **Known residual, not fixed.** IDF v5.5.5 `read_header()` (inside the FIRST `perform()`)
  loops on `-ESP_ERR_HTTP_EAGAIN` until it has the first 1024 body bytes, so a peer silent
  before the first 1 KB of body never returns control to `ota_task`. Closing it needs a
  cross-task socket shutdown into esp_http_client internals, rejected for CRITICAL code at
  this size. The response-header phase is already bounded (`fetch_headers()` timeout →
  `esp_https_ota_begin()` fails). A trickle peer (1 byte every 19 s) counts as progress and
  is out of scope ("stops making progress").
- **Accepted divergence: the simulator is unchanged.** `simulator/device.py`'s
  `urlopen(timeout=30)` already ends a silent download as `download failed: TimeoutError`.
  No server logic depends on the detail text.
- **Rejected.** A server-side expiry for a row stuck at `downloading` (rule 1 in
  `deploys.py`, rejected twice before); changing `OTA_HTTP_TIMEOUT_MS` or keepalive; a
  Kconfig/sdkconfig knob (`sdkconfig.defaults` is CRITICAL, and this is a firmware constant
  like the others).
- **Spec proposal (not applied).** `spec/device-protocol.md` → `dn/cmd` `stage`: "A device
  that receives no artifact bytes for 60 s abandons the download and reports `failed`
  (detail `download stalled`). The update slot is free again for the next `stage`."

---

## 2026-10-03 — a flaky link cannot outrun the confirm timer; a silent one can hold a download forever (R2-test-2)

**Decided: the flaky-radio question is answered, and the answer is structural. The
download and the confirm timer never overlap. One defect (D3 → R2-fw-5) and one reporting
defect (R2-fw-6) are filed, not fixed here.** Proof status: **proven in QEMU (esp32),
bench replay owed** (`docs/runbooks/rollback-test.md` → *Marginal radio*). Transcripts and
numbers: `docs/features/ota-deploy.md` → *Flaky link (R2-test-2)*. No firmware changed.

- **Why the timer cannot fire during a download (two-sided).**
  - The download runs on the running image, which is VALID or UNDEFINED.
    `ff_mqtt_arm_confirm_timer()` arms only when `pending_verify()` is true (`ff_mqtt.c`),
    so no timer exists during a download. QEMU D1: a 124.7 s throttled download, no
    `OTA boot` line, no `no working session`.
  - R2-fw-2: `ff_ota.c::choose_target_slot()` refuses a `stage` while the running image is
    `PENDING_VERIFY`, before any I/O. So no download ever runs inside a confirm window.
- **Where a flaky link does bite: after the reboot.** The new image must reach its broker
  session within 300 s of its first instruction (R2-fw-4). Measured:
  - P1: a 200 s outage confirmed ~0.1 s after the link returned.
  - P2: a 330 s outage fired the timer at 302.2 s and rolled back to `INVALID` +
    `rolled_back`.
  - P3: 5 s up / 25 s down confirmed in the first up-window.
  - esp-mqtt retries every 20–25 s during an outage, and the boot path spends ~10 s in two
    5 s progress-report timeouts. The threshold is therefore: an outage that ends later than
    ≈ 300 s minus one retry gap (≈ 285 s) after boot rolls a GOOD image back. That is a
    miss, not a brick (R2-fw-4 accepted change 2).
- **D3: a silent peer holds the update slot.** A store connection that stays open and
  silent for 600 s never ends the download. IDF v5.5.5:
  - `esp_http_client_read()` turns a transport timeout with nothing read into
    `-ESP_ERR_HTTP_EAGAIN`;
  - `esp_https_ota_perform()` turns that into `ESP_ERR_HTTPS_OTA_IN_PROGRESS` with no stall
    counter, logged at debug only;
  - `ff_ota.c` loops while IN_PROGRESS with no deadline. `s_running` stays set, so every
    other stage gets `another update is already in progress`. Only the proxy's reset ended
    it (D4: `download failed`, board unchanged, next deploy fine).
  - Real hardware may differ: `keep_alive_enable` (IDF 5 s / 5 s / 3) can close a socket
    whose radio is really gone and make it D4. QEMU cannot show that (slirp answers the
    keepalives), so it is the bench question.
- **Re-POST finding (R2-fw-6).** The agent deduplicates on the last command id only. After
  any other command, a re-delivered in-flight stage gets `failed` / `another update is
  already in progress` against **its own** cmd_id. The server then holds a terminal
  `failed` for a download that is still running, and it drops the real outcome when that
  arrives (D3: the later `download failed` was dropped).
- **The tool.** `agent/tools/flaky_link.py` + `just agent-qemu-flaky`, a host-side asyncio
  proxy with `pass`, `throttle:N`, `blackhole` and `reset`.
  - **Its limit:** slirp is the guest's TCP peer, so every outage reads "far end alive but
    silent". No lwIP loss or retransmission, no disassociation, no DHCP, no TX ladder.
  - **A fidelity rule learned in P1:** a connection the client abandons during a blackhole
    must never be replayed upstream. The first P1 run replayed eight stale MQTT CONNECTs,
    which took over the live session and cost ~10 s.
- **Harness artifact, not filed as product: the openeth panic.** One D2 run of three hit
  `Cache error` in `emac_opencores_isr_handler` (`esp_eth_mac_openeth.c:66`). The cause is
  an `ESP_EARLY_LOGW` with a format string in flash, run during an OTA flash write.
  openeth is QEMU-only. Documented in `docs/runbooks/agent-qemu.md`.
- **Rejected: fixing D3 here.** `ff_ota.c` is CRITICAL, and the fix needs its own plan and
  review (R2-fw-5).
- **Rejected: a server-side expiry for a row stuck at `downloading`.** Rule 1 in
  `deploys.py` (R2-test-1 rejected it too).
- **What it changes about one board at a time.** The flaky radio is retired as the reason,
  in QEMU, with the bench owed. One board at a time **still holds**: an image that boots,
  confirms and is broken anyway is recovered by nothing. D3 is a second, liveness-only
  reason to keep USB in reach until R2-fw-5 lands.
- **Spec proposal (not applied), only because D3 held.** `spec/device-protocol.md` →
  `dn/cmd` `stage`: "A device that receives no artifact bytes for N s abandons the download
  and reports `failed`."

---

## 2026-10-03 — the confirm timer is armed before anything in app_main can wait forever (R2-fw-4)

**Decided: `ff_mqtt_arm_confirm_timer()` is the first statement of `app_main`, ahead of
`log_power_fault()`, `log_boot_facts()` and the fault hook. The timer code stays in
`ff_mqtt.c`, and `ff_mqtt_run()` no longer arms it.** Agent 0.4.3. This fixes F5 of the
R2-test-1 entry below, which stays as written. Proof status: **proven in QEMU (esp32),
bench replay owed** (`docs/runbooks/rollback-test.md` → *Hang before the session*).
Transcripts: `docs/features/ota-deploy.md` → *Arm the confirm timer at boot (R2-fw-4)*.

- **Why arming this early is safe.** Everything the timer needs exists before `app_main`:
  - esp_timer is initialised by IDF startup;
  - `pending_verify()` reads otadata only (`esp_ota_get_running_partition()`,
    `esp_ota_get_state_partition()`), with no NVS and no netif;
  - `confirm_timeout_cb()` reads `s_ctx.session_confirmed` (static, zero) and `s_txn.kind`
    (static `TXN_NONE`). A timeout before `classify_txn()` has run therefore takes the
    **immediate** `esp_ota_mark_app_invalid_rollback_and_reboot()`, which is right: there is
    no session to report on. The image the board returns to classifies `TXN_ROLLED_BACK`
    from the record the *previous* image wrote at `staged`, and reports `rolled_back`;
  - `rollback_report_task()` already copes with no client (`s_ctx.connected`,
    `enqueue_status()` checks `s_ctx.client`).
- **Two small additions inside the arm**, logging and bookkeeping only, no decision changed:
  an idempotence guard (`s_confirm_armed`; a second call would leak a second one-shot
  timer), and an `ESP_LOGE` when the confirm timer cannot be created, which used to be
  silent. `confirm_timeout_cb`, `confirm_this_image`, `rollback_now_cb`,
  `rollback_report_task`, `classify_txn` and `ff_ota.c` are unchanged.
- **Rejected: moving `classify_txn()` / `ff_txn_init()` ahead of `ff_net_bring_up`** (R2-test-1's
  suggestion). They need NVS and cannot come before `nvs_ready()`. The grace/report path
  only helps when a session exists; with none, the immediate rollback is already right.
- **Rejected: a new `ff_confirm.c`.** It moves CRITICAL code across files for no behavioural
  gain, and the R2-be-1 tripwires in `tests/test_agent_txn.py` read `ff_mqtt.c`.
- **Rejected: arming after `log_boot_facts()`.** It breaks the pinned "only whitespace
  between `log_boot_facts();` and the hook". First statement also makes "before anything"
  provable by a one-line test.
- **Considered, NOT enabled: `CONFIG_ESP_TASK_WDT_PANIC=y`.**
  - It only adds coverage for a wedge that starves IDLE, and the esp_timer path already
    survives almost all of those: the esp_timer task runs at priority 22, above main (1),
    mqtt (5) and lwIP (18), so a busy-looping or deadlocked task below 22 does not stop
    `confirm_timeout_cb`. A wedge with interrupts off is caught by the interrupt WDT, which
    panics by default.
  - It would change `agent/sdkconfig.defaults` (CRITICAL) and `config_sha256`.
  - It makes a *confirmed* image reboot on a starvation event, against agent_main's
    "nothing here reboots on failure".
  - It risks false panics on the single-core targets (C3/C6) at 80 MHz during CPU-bound
    TLS, which has not been measured.
  - Revisit if the bench ever shows a wedge the esp_timer path misses.
- **Accepted divergence: the simulator is not changed.** `simulator/device.py` starts
  `_confirm_deadline` at its first session after the simulated reboot. Its pre-session
  phase is only a broker connect, and no server-side test depends on the difference.
- **Accepted behaviour changes.**
  1. The 300 s budget runs from the moment the image starts executing. Net bring-up
     (`NET_TIMEOUT_MS` 30 s per attempt), SNTP (≤ 15 s) and the TLS handshakes come out of it.
  2. A *good* image that boots while its AP or broker is down for more than 300 s now rolls
     back. Before, it waited in PENDING_VERIFY forever and got 300 s after the network came
     back. `spec/prd.md`: "rollback counts as a save". The deploy ends `rolled_back`, a miss
     rather than a brick.
  3. An OTA'd image that `park()`s now rolls back after 300 s. A serially flashed board
     (otadata UNDEFINED) and a confirmed one (VALID) still never reboot on failure: the
     timer is armed only on `pending_verify()`.
  4. `FF_ROLLBACK_TEST`'s 60 s also counts from boot. QEMU and the bench reach the session
     well inside it, so the rbtest still logs `confirming` first. If it ever does not, the
     rollback still happens, without `confirming`/`rolling_back`.
- **Spec proposal (not applied).** `spec/device-protocol.md` → `dn/cmd` →
  `confirm_timeout_s`, and `spec/prd.md` → *Requirements & targets* → "Confirm timeout
  (device-armed, default) 300 s": "Counted from the moment the new image starts executing,
  not from its first connect attempt. An image that cannot reach its session for any reason
  rolls back when it expires." This is R2-test-1's proposal, now implemented.

---

## 2026-10-03 — a boot loop and a power cut both land on the previous image; a hang before the session does not (R2-test-1)

**Decided: the remaining failure modes are proven with real OTA'd fault images and the one
state QEMU cannot produce (a torn otadata sector) is written offline. Nothing in the
confirm/rollback path changes.** Proof status: **proven in QEMU (esp32), bench replay
owed** (`docs/runbooks/rollback-test.md`). Transcripts: `docs/features/ota-deploy.md` →
*Remaining failure modes (R2-test-1)*.

- **`FF_FAULT_TEST=bootloop|hang`** (`agent/CMakeLists.txt`, `agent/main/CMakeLists.txt`,
  `agent/Dockerfile`, empty by default). It follows the `FF_ROLLBACK_TEST` pattern: it renames
  the build `-bltest` / `-hangtest`, and any other value is a FATAL_ERROR. It is
  **exclusive with `FF_ROLLBACK_TEST`**: one fault per image, or a result cannot be
  attributed. sdkconfig is not touched, and `config_sha256` equals the normal build's.
  Normal builds compile none of it (the size budgets did not move, and version stays 0.4.2).
- **The hook is in `agent_main.c` only**, between `log_boot_facts()` and `nvs_ready()`.
  The transcript names the slot and its state first, and then none of our code runs: no
  NVS write, no network, no `ff_txn` touch. `ff_mqtt.c` and `ff_ota.c` are not edited.
  `tests/test_agent_fault_injection.py` pins all of this.
- **`firmware/publish.py` refuses `-rbtest`, `-bltest` and `-hangtest`** before any store
  write. A serially flashed image boots UNDEFINED with no rollback armed. A `-bltest` from
  the flasher catalog would therefore abort forever on every new board, and that is far
  worse than `-rbtest` (which merely never confirms). `POST /v1/artifact` still takes
  these versions; that is how they are deployed.
- **IDF v5.5.5 rewrites otadata IN PLACE.** `app_update/esp_ota_ops.c`:
  `esp_ota_current_ota_is_workable()` (behind mark-valid and mark-invalid) calls
  `rewrite_ota_seq()` on the **active** sector. That is an erase of the 4 KB sector, then a
  32-byte program. `bootloader_support/src/bootloader_utility.c` `write_otadata()` does the
  same for NEW → PENDING_VERIFY (the active sector) and PENDING_VERIFY → ABORTED. Only
  `esp_rewrite_ota_data()` (`finish()`) aims at the inactive sector. A cut inside an
  in-place write leaves the other sector, which names the image that ran when the stage
  happened. F4 proves the board lands there, with one boot and no loop.
- **And at the start of every stage.** `esp_ota_begin()` calls
  `esp_ota_invalidate_inactive_ota_data_slot()`, which erases the inactive sector when it
  names a non-running slot. "otadata byte-identical after a mid-download cut" is therefore
  true of the **active** sector only. Safe: the erased entry never names the running image.
- **Accepted liveness gaps.** After F2, F3 or F4 the server row stops at `downloading`,
  `verifying` or `staged`. F4's record names a slot with no INVALID/ABORTED entry, so
  `classify_txn` discards it as stale, by design. A cut during mark-valid (F4d) silently
  reverts a board that was about to confirm. **Rejected: a server-side expiry** that
  closes stale rows. `deploys.py` rule 1 says the server records only what a board said. A
  repeat deploy within the URL TTL reuses the cmd_id and finishes the job (shown in F2).
- **F5, the gap: filed as R2-fw-4 (P0).** The confirm timer is armed only in
  `ff_mqtt_run()`. An OTA'd image that never gets there (network bring-up and enrollment
  retry forever, `park()` loops forever, and `CONFIG_ESP_TASK_WDT_PANIC` is off) stays
  PENDING_VERIFY until a human power-cycles it. In QEMU the `-hangtest` image ran 333 s with
  no reset, and only the next power cycle rolled it back. Not fixed here: it is the
  CRITICAL path and needs its own plan and review.
- **The emulation's limit.** A SIGKILL is a power cut between SPI flash commands, never
  inside one, so a torn page is never produced live. `just agent-qemu-otadata … tear` writes
  the two shapes an erase-then-program cut can leave (the sector erased, or the entry
  without its crc word), and nothing else.
- **Spec proposal (not applied), for R2-fw-4.** In `spec/device-protocol.md` →
  `confirm_timeout_s`: "counted from the moment the new image starts executing, not from
  its first connect attempt; an image that cannot reach its session for any reason rolls
  back when it expires."

---

## 2026-10-03 — the dashboard says "good" only while the board's announce agrees (R2-fe-1)

**Decided: a finished deploy gets a verdict word (`good`, `rolled back`), gated on the
server's `is_terminal` plus a lookup (`deploy.ts::DEPLOY_OUTCOMES`). `good` is shown only
while `device.fw_version` equals the confirmed artifact; otherwise the cell reads
`confirmed by the board` with a drift line. `back on {from}` is claimed only when the announce
equals `from`.** Frontend only, no API change.

- Drift drops the verdict rather than becoming `rolled back`: the CUJ-1 hard-fail trap
  ("a milestone shown as reached while no longer true"), and the client must not author an
  outcome the server did not record.
- **Rejected:** a server-side `last_outcome` that survives a new in-flight deploy. It is an
  API change for a ~5-board, one-at-a-time fleet. Revisit with group deploys / the KPI view (R6).
- **Rejected:** inferring an outcome for a `rebooting` row from `fw_version` (the R1-to-R2
  transition gap). Same rule, applied to the client.
- **Gotcha:** the dev `frontend` container can exist on the production nginx image, which
  serves a stale bundle. `docker compose up -d --no-deps --build frontend` restores Vite.

---

## 2026-10-03 — a board never writes a slot the boot pointer names, and never stages over an unconfirmed image (R2-fw-2)

**Decided: `ff_ota.c::choose_target_slot()` chooses the slot once, before any I/O, and
refuses two stages. One arrives while the running image is `PENDING_VERIFY`. The other
arrives while the boot pointer names a slot other than the running one, which means an
earlier staged image is waiting for a reboot. That slot is handed to esp_https_ota as
`.partition.staging`. `restore_boot_partition()` writes otadata only if the pointer
actually moved.** Agent 0.4.2. This **resolves** the R2-be-1 entry's *Out of scope,
noted*, which stays as written.

- **The atomic switch is IDF's, and it is conditional.** otadata is two 4 KB sectors with
  one `{ota_seq, label, ota_state, crc}` entry each. `esp_rewrite_ota_data()` writes the new
  entry into the sector that is not active (`next = ~active & 1`). A torn write leaves a
  bad CRC there, and the bootloader keeps the still-valid active sector. **This holds only
  while the two sectors name different slots.**
- **G2, the equal-seq rewrite (IDF v5.5.5 `app_update/esp_ota_ops.c`).** The new seq comes
  from `while (seq > (id+1)%N + i*N) i++`, and equality stops it. Switching to the slot the
  active entry ALREADY names therefore returns the same seq and writes it into the other
  sector, which held the running image's entry. After an `apply: "on_command"` stage
  (active = `seq n+1 → T`, other = `seq n → R`), a second stage of a different artifact
  passes every IDF check: T is not running, R is VALID, and `invalidate_inactive` skips a
  sector that names the running slot. It erases T under the active entry. Its `finish()`
  then leaves both sectors at `seq n+1 → T`. If T fails to confirm, the rollback boots T
  again, and the next timeout gets `ESP_ERR_OTA_ROLLBACK_FAILED`. The board is stuck on a
  broken image. That is the CRITICAL.md failure.
- **G4, the same mechanism in the undo.** A failed `finish()` (`esp_ota_end` →
  `ESP_ERR_OTA_VALIDATE_FAILED`) never reaches `esp_ota_set_boot_partition()`. The old undo
  then called `set_boot_partition(running)` anyway. That wrote a duplicate `seq n → R`
  entry in state NEW over the previous image's entry. Depending on sector parity, the
  known-good image then boots `PENDING_VERIFY`. After D2, a failed `finish()` leaves
  `boot == running` in every non-torn case, and the undo logs `nothing to undo`.
- **G1.** IDF's own `PENDING_VERIFY` refusal lives in the first `perform()`. That comes
  after `downloading` and the signed-URL fetch, and it was reported as `"download failed"`.
  Ours runs first and says `"the running image is not confirmed yet"`. It refuses on exactly
  `PENDING_VERIFY`. A failed state read (`ESP_ERR_NOT_FOUND`, which every serially flashed
  board returns) is allowed, and IDF's check stays the backstop.
- **G3.** `ff_ota.c` and esp_https_ota each picked a slot. They agreed, but nothing enforced
  it. Now there is one pointer, and `esp_ota_get_next_update_partition(` appears once in the
  file.
- **The refusals are read-only.** They make no otadata write and no erase, and they never
  call `ff_txn`. The earlier stage's record stays valid: its image boots at the next reset
  and reports against its own cmd_id.
- **Rejected: supersede** (move otadata back to R, clear the record, write T). It costs two
  otadata writes and puts the good image into NEW/`PENDING_VERIFY` (G4's problem). It also
  orphans the earlier cmd_id at `staged`. "Reboot it first" is the honest answer, and R1
  has no `apply` command.
- **Accepted gap (liveness, not safety).** `boot != running` also happens when the
  bootloader rejected a staged image and fell back to R without rewriting otadata. The
  board refuses stages with the same detail until one reset turns that entry `ABORTED`.
  That case needs flash damage after both our sha256 check and IDF's validation passed.
- **Simulator parity** (`simulator/device.py::StageRunner`). It applies the same two
  refusals, after `staging` and before the size guard, with the same details. `pending`
  covers the first. A new `staged` field, which nothing in the simulator clears, covers the
  second, because the simulator has no power cycle and no `apply`.
- **Spec proposal (not applied).** For `spec/device-protocol.md` → `dn/cmd`: a device
  refuses a `stage` with `failed`, before downloading anything, while its running image has
  not yet confirmed, or while an image it staged earlier waits for a reboot.

**Proof status: proven in QEMU (esp32, dev stack).** The negative control on 0.4.1 showed
both gaps in the otadata decode. After a failed `finish()` (one flipped byte, `image
validation failed`, `boot partition put back to ota_0`), sector1 held a duplicate
`seq=1 -> ota_0 NEW` next to sector0's `seq=1 -> ota_0 VALID` (G4). After an
`on_command` stage of 0.4.5 and a second `on_command` stage of 0.4.3, which re-downloaded
into ota_1, **both sectors read `seq=2 -> ota_1 NEW`**, and the running ota_0's entry was
gone (G2). On 0.4.2 the same failed finish logged `nothing to undo` and left otadata
byte-identical. The second stage was refused with zero artifact GETs and otadata kept two
distinct seqs (`seq=1 -> ota_0 VALID`, `seq=2 -> ota_1 NEW`). A stage during an rbtest
image's confirm window was refused with zero GETs, and the rollback still landed on ota_1.
T2 ran with the api recreated on the `.env.example` dev hash; `.env` untouched. The
transcripts are in `docs/features/ota-deploy.md` → R2-fw-2.

## 2026-10-03 — the digest is checked before the boot pointer moves (R2-fw-1)

**Decided: `ff_ota.c::ota_task` reads the slot back and compares the sha256 BEFORE
`esp_https_ota_finish()`. A mismatch is an `esp_https_ota_abort()`, and otadata is never
written.** This **supersedes** two points of R1-fw-1 §2 (2026-09-17), which stays as
written: the claim that `esp_ota_write` withholds the header's first 16 bytes until the
write completes, and the rule that a mismatch restores the boot partition. Agent 0.4.1.

- **The window that existed.** `finish()` is `esp_ota_end()` plus
  `esp_ota_set_boot_partition()`. The R1 order hashed after it and undid the switch on a
  mismatch. Between the two, while up to 1.9 MB was read back and hashed, otadata named an
  unverified image. A power cut, a brownout (S0-fw-3) or a watchdog in that window boots
  it. In QEMU the 0.4.0 agent held the bad pointer for about 8 s (37.3 s → 45.6 s in the
  T2-0 log). Each mismatch also rewrote otadata twice.
- **The IDF evidence (v5.5.5, re-read for this task).** In `app_update/esp_ota_ops.c`,
  `esp_ota_write()` buffers bytes in `partial_data[16]` only inside
  `if (esp_flash_encryption_enabled())`, and what it holds back is the TRAILING partial
  block. `esp_ota_end()` writes that block only when `partial_bytes > 0`, then runs
  `ota_verify_partition()`. `esp_https_ota.c::_ota_write()` passes each chunk straight to
  `esp_ota_write()`. So without encryption, every byte is on flash once `perform()`
  returns ESP_OK. The T2 run confirms it: the pre-`finish()` read-back hashed to exactly
  the artifact's sha256 (`5d2e6347…`).
- **No second hash after `finish()`.** Without encryption, `esp_ota_end()` writes nothing
  more to the slot, and its `ota_verify_partition()` already re-reads the image against
  IDF's appended SHA-256. `restore_boot_partition()` stays, but only on the failed-`finish()`
  path. It is now the one call to `esp_ota_set_boot_partition()` in the file.
- **`#if CONFIG_SECURE_FLASH_ENC_ENABLED` → `#error`** in `ff_ota.c`, so the premise
  cannot rot silently. `verify_bundle.py` already rejects encryption fleet-wide. There is
  no runtime check, because a board with encryption eFuses and our plaintext bootloader
  does not boot at all.
- **`ff_txn_save` stays after `finish()`.** The transaction becomes live when the boot
  pointer moves (R2-be-1). A record saved ahead of a `finish()` that fails would name a
  slot that is not bootable.
- **The command seam accepts `artifact.sha256` only as 64 lowercase hex**
  (`ff_mqtt.c::on_stage`, `is_lowercase_sha256`). The value is never normalised (the
  `identity.py` idiom). Uppercase, short, too long or non-hex gets `failed` /
  `artifact sha256 malformed` with no `staging` and no I/O. Before, such a digest cost an
  erase of the spare slot (the previous good image) and a full download. The comparison in
  `ff_ota.c` is now an exact `strcmp`.
- **Slot-size guard.** `cmd->size > target->size` publishes `failed` /
  `artifact larger than the ota slot` before `resolve_artifact_url` and before
  `esp_https_ota_begin`. That means nothing is fetched or erased. On the wire it comes
  after `staging`, because the slot is resolved after that publish.
- **Simulator parity** (`simulator/device.py::_stage`). It applies the same two refusals,
  in the same order, with the same details. The regex is local, because simulator import
  purity is enforced.

**Proven in QEMU (T2).** 0.4.0 + corrupt stage: `boot partition put back`, and otadata
changed. 0.4.1 + the same stage: `… was never moved`, and otadata was byte-identical
(8192 bytes, `cmp`). Uppercase and `abc` digests got one `failed` each and no
`GET /v1/artifact`. 1966081 bytes got `staging, failed`, again with no GET. The happy path
gave `matches … switching` → `ff-txn … recorded` → `staged and bootable` →
`confirmed|t`. Transcripts are in `docs/features/ota-deploy.md`.

**Spec proposal (not applied; `spec/` is protected).** `spec/device-protocol.md` →
`dn/cmd`: "`artifact.sha256` is exactly 64 lowercase hex characters. A device refuses any
other spelling with `failed` before downloading, and refuses an `artifact.size` larger
than its `ota_slot_size` before writing."

---

## 2026-10-03 — the deploy outcome is device-reported, from a transaction record that survives the reboot (R2-be-1)

**Decided: the agent persists ONE record across the apply reboot, `(cmd_id, target slot
address)`, and the outcome is reported by the session that observed it.** This
**supersedes** the "nothing is persisted across the reboot" rule in the 2026-09-17 entries
(R1-fw-1 §1, restated in R1-fw-2 §3). Those entries stay as written. Without the record
the `cmd_id` died with the image that received the `stage`, so every deploy parked at
`rebooting` with `is_terminal: false`, and the server could not tell a confirm from a
rollback.

- **Written by `ff_ota.c` at `staged`, not at `applying`.** `esp_https_ota_finish()` has
  already moved the boot pointer by then, so any reset boots the new image. That includes
  `apply: "on_command"` followed by a power cut. The write goes before the `staged and
  bootable` log line, because that line is what the QEMU runbook waits for before pulling
  the plug. The slot logic is unchanged.
- **Its own NVS namespace (`ff_txn`), never `ff`.** `ff_store` erases `ff` on a token
  change, and that rule stays about credentials. Nothing erases the partition (S0-fw-4).
  A save erases the namespace first, so a torn write reads back as "no record", never as an
  old `cmd_id` paired with a new slot.
- **Classified once at boot, from otadata, never from the record alone** (`ff_mqtt.c`
  `classify_txn`). Running the recorded slot in `PENDING_VERIFY` means `confirming`.
  Running it `VALID` means `confirmed` is owed. Running the other slot while
  `esp_ota_get_last_invalid_partition()` names the recorded one means `rolled_back`. This
  covers INVALID (our timer) and ABORTED (the bootloader). Anything else is a stale record:
  it is discarded with a WARN and nothing is reported. A false `rolled_back` would be a lie
  kept forever.
- **`confirmed` is queued only when `esp_ota_mark_app_valid_cancel_rollback()` returned
  ESP_OK**, at the same announce PUBACK as before. The confirm moment did not move.
  `rolled_back` is reported by the image the board **returned to**.
- **The record is cleared at the terminal state's PUBACK, never at enqueue, and only if
  it is still for that `cmd_id`** (`ff_txn_clear_if`). A reset before the PUBACK re-derives
  the same outcome and says it again, and the server deduplicates. A newer `stage` that
  verified in the meantime keeps its own record.
- **`rolling_back` is best-effort and can never delay the rollback.** `confirm_timeout_cb`
  calls no `esp_mqtt_client_*` function. Those take the client's lock, and a broken image
  is exactly the one whose mqtt task may be wedged. The callback starts a pre-created
  `ff_rollback` esp_timer (2 s grace) and spawns a one-shot task that enqueues the report.
  If the timer cannot start, or there is no transaction to report on (an image written by
  an R1 agent), it rolls back immediately, as before. Proven in QEMU: an
  `FF_ROLLBACK_TEST` image put `rolling_back` on the broker and was back on the old slot
  3 s later.
- **The timeout's decision stands through the grace window.** Before, the reboot followed
  the decision within microseconds. Now it can be up to 2 s later, so an announce ack that
  lands in between is logged and does **not** mark the image valid
  (`s_rollback_decided`). Otherwise one transaction could end both `confirmed` and
  `rolled_back`.
- **Outcome publishes are enqueued (`esp_mqtt_client_enqueue`, store=true), not
  published.** They come from the esp-mqtt event handler or from the report task.
  `ff_mqtt_publish_status` keeps its immediate send for ff_ota's task, whose drain timing
  depends on it.

**Rejected:**
- **Inferring `rolled_back` on the server** from an announce whose `fw_version` equals
  `from_version`. It breaks `deploys.py` rule 1: the server never writes a state for a
  transaction the device did receive.
- **The new image reading its own retained `up/status`** to recover the `cmd_id`. That
  needs devices to have read access on `up/`, which is a `mosquitto/acl` change, and the ACL
  is CRITICAL.

**The transition gap, accepted.** Only an image that contains this code writes the record,
and only the image the board returns to can report `rolled_back`. So a deploy issued while
a board runs an R1 agent gets no outcome, even if the new image is R2. That includes prod's
`94a990dd09a4` on 0.3.1. Such a transaction stays at `rebooting`, and a rollback *to* an R1
image is never reported. From the second R2→R2 deploy on, every outcome is reported.

**The simulator models the same thing** (`StageRunner.pending`/`rolled_back`,
`--confirm auto|never`, `--confirm-timeout`). The confirm timer is a session
*sentinel*, not a worker, because it ends the session by setting `reboot`. It is armed
once per boot, so a reconnect resumes it rather than restarting it.

**Out of scope, noted.** A `stage` that arrives while the running image is still
`PENDING_VERIFY` would write the rollback target. IDF's `esp_ota_begin` should refuse it,
and ff_ota reports `failed`. That belongs to R2-fw-2.

Agent `0.4.0`. Details and evidence: `docs/features/ota-deploy.md` → *R2-be-1*.

---

## 2026-10-02 — the flashing bench is Windows + Chrome, not the Mac

**Decided: every host reference to "the Mac" as the bench is obsolete**. The bench is a
Windows machine running Chrome on Windows (not WSL), with the ESP32-S3 on native USB as
COM3. That is where the board enrolled and where `S0-fe-8` was observed.

- Supersedes the "the Mac is the flashing bench" and "The bench is the Mac" lines in the
  entries on the QEMU `esp_task_wdt_init` panic (S0-infra-1) and on S0-test-1 filing.
- `S0-test-1` and `S0-test-2` in `TODO.md` now name Windows. The "release really releases"
  check uses a second terminal on the COM port (for example,PuTTY, 115200), not `screen`.
- The Linux dev box still does not enumerate boards over WebSerial. That stays the same.
- Re-acquire after `hard_reset` is an OS-and-driver property, so record the driver and COM
  port with any bench result.

---

## 2026-10-02 — queued spec proposals applied. `ab-4m-arduino-v1` is a supported layout

**Decided: flush `spec/open-questions.md` before R3**. The answered proposals now live in
the spec text. The questions were deleted from the open list rather than marked resolved.

- **`spec/device-protocol.md`** gains a *Partition layouts* table (`ab-4m-v1`,
  `ab-4m-arduino-v1`). It also states `cmd_id` reuse as the code does it: one id per device
  and artifact while the signed URL is valid, and a new id once it expires. `cmd_id` is necessary on transaction states, recording `(cmd_id, state)` is idempotent. The
  `state` vocabulary is open. `awaiting_safe_window` applies only to devices that have a
  window. `artifact.sig` stays optional until R6.
- **`spec/prd.md`**: one artifact-size limit, the target layout's `ota_slot_size`
  (1,966,080 B for both). Also adds the four chip targets, and renames the swarm section
  to V3.
- **Code: `SUPPORTED_LAYOUTS` gains `ab-4m-arduino-v1`** at the same slot size. Upload,
  the catalog and the bundle loader accept it with no other change. Deploy compatibility
  stays an equality check. Thus, a device on one layout never gets the other's image.
  `EXPECTED_PARTITION_LAYOUT` stays the same, because the prebuilt agent still ships
  `ab-4m-v1`.
- The ADR (`design/decisions/arduino-gets-its-own-layout-id.md`) absorbed the measurements
  as an Appendix, so the evidence survives the open-questions entry being deleted.

**Not applied:** the CUJ-2 proposal in `spec/open-questions.md` (enrollment). It needs a
product call, not a sync.

---

## 2026-10-01 — "no COM port" is answered with words, and COM1 triggers a refusal (S0-fe-8)

**Decided: help text, plus one refusal**. No port filters and no driver detection.
`requestPort()` stays unfiltered. Filtering by USB vendor id would hide nothing that
matters (COM1 is the only stray entry) and would hide a board on an unusual bridge. This is a worse dead end than the one being fixed. The page cannot detect a missing driver
either, because Web Serial only ever sees ports the OS created. So the fix is what the page
*says*: "My board does not appear" (`PortHelp.tsx`).

- **A port with no USB vendor id triggers a refusal before any handshake** (`checkChosenPort`,
  both chooser call sites). That is exactly the "plainly not an ESP32" case. Anything with
  a USB id goes through. An unknown vendor logs rather than blocks.
- **No chip identification step**. "Install both drivers, they do not conflict" beats any
  instruction that needs Device Manager, and the acceptance forbids Device Manager.
- **The help opens itself on a dismissed chooser**. A stuck operator's next move is
  closing an empty-looking chooser. Thus, a collapsed `<details>` alone would wait for a click
  that never comes.

**Gotchas**. The refusal message and the summary must say *the same words*
(`My board isn't listed`, straight apostrophe), because the message tells the operator to
look for it. The Silicon Labs driver page returns a 403 from Akamai to the dev box, so
check that link from a real browser. Bench acceptance is owed and lives in `S0-test-1`.
Details: `docs/features/enrollment.md`.

---

## 2026-10-01 — status lives in TODO.md. Feature files are archives, and a release's tasks wait there (S0-ops-1)

**Decided: `TODO.md` is the only document that says what is open, next or blocked. README
gets one paragraph, and that paragraph points at `TODO.md`**. `docs/roadmap.md` and
`docs/features/*.md` carry no Status headers, columns or "Last Updated". A feature file's
`Target` says which release a thing is *slotted* for. This is a plan, not a state.
Rejected: keeping the status columns "in sync". They drifted within a week (roadmap still
said R0 was gated on `S0-test-3` after it passed), and a reader of the front door bounced
before reaching the product.

- **`TODO.md` carries Sprint 0 plus the *active* release only**. A future release's task
  list lives in its feature file as a plain list (no checkboxes) and moves into `TODO.md`
  when the release opens. R3's list is the first example (`ota-library.md` → *R3 task
  list*). Closed releases leave `TODO.md` entirely. Their substance is in the feature
  files.
- **No active release is a legitimate state**. R1 is complete on metal, but R2 opens only via
  `/replan` passing the CUJ-1 gate, and until then `TODO.md` says so in *Where this
  stands* rather than inventing an R2 section.

**Gotcha**. Archive before you cut. Three closed tasks (`S0-fw-3`, `S0-test-3`,
`R1-test-1`) had no write-up anywhere but `TODO.md`. A strip-first pass would deleted
the two `S0-fw-3` results that must not be re-derived. Grep `docs/features/` for the task
ID before deleting any `TODO.md` entry. Details: `docs/features/infrastructure.md`.

---


## 2026-10-01 — the ingestor's heartbeat is a timer inside the session, with a stall guard (S0-infra-8)

**Decided: touch the liveness file on a 30 s timer that lives only as long as the broker
session, and stop the ticks while a single message was in flight for more than
60 s**. Liveness means "connected and consuming", not "a device spoke recently". A
3–15 board fleet is idle most of the time, and the traffic-keyed probe sat red for 27
hours over a healthy process. That false positive is the worse failure, because it
trains the operator to ignore the one red light.

- **Why not a free-running timer:** it would stay green through a dead broker connection
  and through a wedged consumer. Scoping it to the session handles the first. The stall
  guard handles the second. This is because a hung write keeps the session up.
- **A failed write still counts as alive**. `handle_message` swallows DB errors and
  returns. Thus, the ticks continue. That is deliberate: restarting the ingestor does not
  fix Postgres.
- **Rejected: probing the broker from the healthcheck** (for example,`mosquitto_sub` in the
  container). It would need a credential in the probe and would test the broker, not this
  process.

**Gotchas**. The prod healthcheck (services `prod/docker-compose.yml`) reads red when idle
on any image older than this change. Ship it together with the digest bump. Nothing on
prod acts on container health yet: `healthcheck.sh` is HTTPS-only. Details:
`docs/features/infrastructure.md`.

---

## 2026-10-01 — readiness means "can deploy", and the release gate walks the board's path

**Decided: `/v1/readyz` (and so the API healthcheck, dev and prod) is 503 when any
deploy-mandatory setting is absent**. That covers object store, `ARTIFACT_URL_SECRET`,
`PUBLIC_BASE_URL` and the commander pair. Deploy is the product, so an API that cannot
deploy is not ready. That holds even though the dashboard would render.
Rejected: keeping the startup WARNING as the only signal. F-2026-09-23-001 showed nobody
reads it. `create_app()` still constructs with no env, and `healthz` is still no-I/O.
The settings stay `None`-defaulted. Only the probe's verdict changed (S0-infra-9).

- **Config presence only, no I/O, in the probe**. Readiness runs every 10 s on every
  replica. Minting and getting a blob there would bill GCS for a heartbeat and couple
  rollout to the store's latency. Proof that the path *works* belongs in the release gate
  (`just deploy-check`), not the probe.
- **Names, never values**. The probe is public behind nginx. `missing` lists env names.
- **Consequence, accepted:** `frontend` has `depends_on: api: service_healthy`. Thus, a
  misconfigured api keeps the dashboard down on a fresh `up`. A dashboard whose one job
  is dead was the bug. A runtime gap does not take a running frontend down (Traefik routes
  only to the frontend).
- **`deploy-check` asks the API's readyz through `PUBLIC_BASE_URL` before anything else**.
  The download endpoint never reads `PUBLIC_BASE_URL`. Only the deploy that mints the link
  does. A host-side check signing with the host's env would pass against an API missing
  it. This is F-001 exactly. Step 0 is the only part of the check that sees the API's own
  env.
- **Broker liveness stays out of readyz**. Config present + dynsec missing in memory
  (F-002) can only be caught by connecting as the commander. The gate chains
  `broker selftest` after the download check rather than importing it, so each keeps its
  own transcript.

**Gotchas**. `minio/minio` and `minio/mc` no longer pulls (Docker Hub and
quay.io). Thus, a dev box with pruned images cannot `just up`. T2 ran on Chainguard images
via an uncommitted override. That needs its own task. Dev's API origin is
`localhost:8088` (`FF_HTTP_PORT`). `:8080` on the dev box is SearXNG. `just
deploy-check-prod` runs `python -m fleetforge.deploycheck` inside the prod image. Thus, it
only works once a build containing this module deploys. `services/scripts/deploy.sh`
still smoke-tests `/v1/healthz`. Details: `docs/features/infrastructure.md`.

---

## 2026-09-23 — a simulated apply ends the session, because that is what a reboot is

**Decided: fix CUJ-1 segment 5 by giving the simulator a reboot, not by making the live
session tell the truth about its identity**. The tempting one-liner was to let the
heartbeat loop read `stage.identity` on every beat. Thus, the new `fw_version` appears
without a reconnect. Rejected: it would make the gate pass while modeling a board that
does not exist. A real ESP32 cannot change the version it is reporting without restarting
. The announce/reconnect boundary is the *only* place a version legitimately changes, and
`ingestor` presence, `last_seen` and the deploy timeline are all shaped around a board
that goes away for a few seconds. Fidelity here is the product. The simulator's whole
value is that a pass against it means something about metal. `R1-test-1` passed on
metal through exactly this path (0.3.2 → 0.3.1) while the sim silently did not.

**The defect is worth remembering more than the fix is**. `_stage` rebound
`StageRunner.identity` and logged "announced on the next connect". True, honest, and
never checked — on an `always_on` board the next connect never comes, because nothing
ended the session. The write-up is in `docs/features/ota-deploy.md`. The shape is
F-2026-09-23-001/002 again (`../docs/ops-log.md`): *a component reporting healthy because
nothing exercises the one path that is broken*. Two signals were pointing at it and
nobody believed either one. The R1-fe-1 acceptance script already carried a note that the
version only flips if you `docker compose restart mosquitto`, and that note also contained
a wrong explanation (that heartbeats do not carry `fw_version`, they do, they were
carrying the stale one). **A manual step in an acceptance script that stands in for
something the product must do by itself is a bug report.**

**Learnings for whoever touches the simulator next.**

* A reboot is a *third* kind of session end. It is worth keeping distinct from the
  other two. Not an error, not the broker closing the stream: a scheduled absence. It
  neither grows nor resets the reconnect backoff, and it resets `boot_monotonic` because
  a rebooted board's uptime starts over.
* `tests/test_simulator.py` never drove `run_always_on` or `run_sleepy` at all.
  Every test called `run_session` directly. This is precisely the layer that cannot
  observe a reconnect. The four new tests are the first to hold a reconnect loop.
* The remaining infidelity goes into the record, not hidden. The reboot exits through a clean
  DISCONNECT. Thus, the LWT does not fire and retained presence stays `online` across the
  boot. `aiomqtt` has no public API for an ungraceful drop (property 5), and `--crash-after`
  remains the only honest way to exercise a real will.

---

## 2026-09-23 — auto-rollback proven on metal. Remote firmware work has no blockers

**The board saved itself**. We deployed to device `94a990dd09a4` a deliberately broken
`0.3.2-rbtest` image, booted it, joined the fleet on it, and 71 s later returned on 0.3.1
with nobody touching it. That is `confirm_timeout_cb()` →
`esp_ota_mark_app_invalid_rollback_and_reboot()` running on real hardware for the first
time in the product's life. CRITICAL.md calls this path *"the whole bricking gamble... the
one failure the product must never have"*. It is no longer untested.
Procedure: [`docs/runbooks/rollback-test.md`](docs/runbooks/rollback-test.md).

**Decided: test it with a build flag, not a throwaway patch**. `FF_ROLLBACK_TEST` is off
by default and compiled out of normal builds entirely. It injects exactly one fault (the
announce's `msg_id` drops so its PUBACK can never match and `session_confirmed`
stays false) and shortens `CONFIRM_TIMEOUT_S` to 60 s. The announce still publishes with the retain flag. Thus, the board genuinely joins the fleet on the bad version, which is
what makes the rollback observable in the dashboard rather than only on a console. The
flag renames the build to `<version>-rbtest`, which travels into every announce.

Two properties make the result transferable rather than a curiosity. The resolved
sdkconfig is **byte-identical** to a normal build (`config_sha256 d10f52d6…`). Thus, the
bootloader posture under test is production's. And a default build of the same commit
produces `0.3.2` with zero `rbtest` strings. Thus, the flag cannot leak into a shipped image.
The one rule in the runbook: build to a scratch dir and upload as an **artifact** only —
never `agent-publish`, which writes the USB flasher catalog.

**Consequence: the blanket "deploy only to a board you can physically reach" retires in favor of with a per-failure-mode rule**. Of the three modes, a non-booting
image is the bootloader's job (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`, on everywhere),
"boots but never confirms" is now proven recoverable. The residual gamble is an image
that *does* get its announce acked and is broken in some other way. It confirms itself
and nothing rescues it. That third case is what R2's checksum gate (`R2-FW-3`) and
server-side confirm observation (`R2-BE-1`) are for. Until then: remote deploys are a
reasonable risk, one board at a time, never a fleet-wide roll.

This is what actually unblocks remote work. Before today the only enrolled board was one
bad push away from needing a bench visit. Thus, all firmware iteration was bench-bound. Now
R2 development and radio-dependent testing can be driven remotely.

---

## 2026-09-23 — the broker bootstrap is two phases, and they must stay separate

**Implements the decision below**, which called for `mosquitto-init` to be rewritten
against the live control topic. Shipped as *two* one-shot containers rather than one
rewritten container, because the two jobs have opposite ordering requirements and that
is the whole point:

* **`mosquitto-init` → `mosquitto/bootstrap.sh`, before the broker**. Creates
  `dynamic-security.json` with `dynsec init` if absent, fixes uid-1883 ownership, and
  stops. It has to run first. This is because the dynsec plugin refuses to load without the
  file. `dynsec init` is the only `mosquitto_ctrl` subcommand that works on one.
  It is no longer given the ingestor or commander credentials at all — a service
  credential handed to this phase is a credential written to a file the running broker
  will overwrite.
* **`mosquitto-config` → `mosquitto/configure.sh`, after the broker is healthy**. Every
  role, client and default-ACL, applied over `$CONTROL/dynamic-security/v1`. It has to
  run second. This is because that interface needs a live broker.

**Why not one container that waits for the broker**. The broker `depends_on` the file
existing. Thus, a single container would have to be both before and after it. Splitting is
the only shape that is not a cycle.

**The idempotency this design enables, and three ways the old script faked it**.
`configure.sh` re-runs on every `up` and every deploy. Thus, it converges the broker's
memory with its file. That is why deploying it is also the *fix* for a broker that
drifted, with no restart. That only holds if every command is honestly
idempotent:

1. The old `ctrl` helper ended in `|| true`. It swallowed every refusal, which is how a
   bootstrap that configured nothing still logged a clean run. Any unexpected output
   is now fatal. The *output* is what we check, never the exit status, because
   `mosquitto_ctrl` **exits 0 on a refused command** (checked against 2.0.22).
2. `addClientRole` has no idempotent form. On a client that already holds the role it
   answers `Error: Internal error`, which cannot be told apart from a real internal
   error. Thus, it cannot go in a tolerated-error branch. Role grants read the client's
   roles back first instead. `createRole`, `createClient` and `addRoleACL` do all answer
   "already exists" and are handled that way.
3. Nothing ever read the result back. The script now ends by getting all five roles and
   clients out of the broker's memory and failing if any is missing. The assertion
   F-2026-09-23-002 got past for five days.

**`deploy.sh` must list `mosquitto-config` in `INFRA_SERVICES` explicitly**. It depends
*on* mosquitto rather than the other way round, so `docker compose up -d mosquitto` does
not pull it in. Only the ingestor `depends_on` it, and deliberately not the API: the API
authenticates as the dynsec admin, which phase 1 creates, and a one-shot in
`docker rollout`'s path buys nothing.

**Verification**. The dev broker was put into prod's exact state — `deleteClient
ff-commander` + `deleteRole commander` against the *running* broker — and reproduced the
symptom (`mosquitto_pub -u ff-commander` → CONNACK 135, while the on-disk file was
irrelevant to it). Re-running `mosquitto-config` restored it to CONNACK 0 with no broker
restart, and `just broker-check` returned `SELFTEST OK` across the full ACL matrix.
Separately: a fresh volume configures correctly from both phases, the estate survives a
broker restart, a wrong admin password exits 1 with the rotation hint, and a re-run
against a fully configured broker is silent. `tests/test_broker_config.py` guards the
split in both directions.

## 2026-09-23 — R1 closed on hardware. Bootstrap must drive the live broker, not its file

**OTA works on metal**. `R1-test-1` passed: device `94a990dd09a4` (ESP32-S3) went
`0.3.2 → 0.3.1` on a deploy driven from the dashboard API — `downloading` → `rebooting` →
back online on the new version in ~25 s. R1 is complete. Write-up in
[`docs/features/ota-deploy.md`](docs/features/ota-deploy.md).

**Reaching that took three production fixes, and the pattern in them matters more than
any one of them**. Full detail in `../docs/ops-log.md` F-2026-09-23-001/002/003.

**Decided: `mosquitto-init` is wrong by construction and must be rewritten to use the
live control topic**. It currently starts a *throwaway* broker on port 1884, applies
its dynsec commands there, writes `dynamic-security.json` into the shared volume and
exits. But `fleetforge-mosquitto` is `restart: always`, reads that file **once at
startup**, and is authoritative in memory thereafter — so on any established deployment
the bootstrap writes underneath a broker that will never read it. Confirmed empirically:
`listClients` on the live broker returned only `94a990dd09a4`, `ff-admin` and
`ff-ingestor`, with no `commander` client and no `commander` role, while the on-disk file
contained both with a password hash that checks correctly against `prod/.env`. The
bootstrap has, as far as we can tell, never once taken effect on prod.

Two consequences that make this worse than inert:

1. **It is silently destructive**. The live broker rewrites that file from its own memory
   on any dynsec change — and per-device enrollment causes one. So a bootstrapped entry is
   not merely ignored, it is scheduled for deletion.
2. **Restarting the broker is not the fix**. Shutdown can persist stale state over the
   good file first. This would make the bootstrap's write disappear rather than take.

The interface to use already exists and is already proven. The API creates per-device
credentials over `$CONTROL/dynamic-security/v1` on the running broker. That is why
enrollment has always worked while bootstrap never has. `mosquitto-init` must do the
same, and must be idempotent against a broker that already holds the entries. Tracked
as services `S0-infra` work. The immediate unblock was `createRole`/`addRoleACL`/
`createClient`/`addClientRole` issued live, which converges memory and file with no
outage.

**Learning: the hardware E2E is not a checkbox, it is the only integration test of the
deploy chain**. Two of the three defects meant `POST /v1/devices/{id}/deploy` had **never**
succeeded in production, and nothing else in the system could revealed that.
`/v1/healthz` was green, the dashboard rendered a working Deploy button, unit and QEMU
coverage all passed. Keeping `R1-test-1` bench-gated rather than redefining it to
something the emulator could pass (decided 2026-09-16) is precisely what caught this.
A QEMU version of the test would passed against a broken production. This is the
same shape as ops-log F-2026-09-20-004/005/007 (a component reporting healthy because
nothing exercises the one path that is broken) now four times in four days.

**Corollary for how these tasks get written**. `R1-test-1` said "the target is already on
the fleet. Thus, the run is a deploy from the dashboard and a version check". That was
wrong: the enrolled board was on agent 0.2.0, which predates the OTA capability. Thus, a
bootstrap USB re-flash was mandatory first. Future hardware E2E tasks must be written
as *bootstrap-flash then deploy*, and must not be costed as cheap.

---

## 2026-09-22 — R0-test-2 passed on an S3. S0-fw-3 is a defect, not a gate

**The product works on metal**. On 2026-09-19, device `94a990dd09a4` — an **ESP32-S3** —
flashed from `bingo.tvaroska.sk` with the v0.2.0 agent (`d705652`) ran the full ladder in
13 s (`link_up → time_synced → enrolling → enrolled → mqtt_connected`) and stayed live
66 s past enrollment. That is R0's "Done when" verbatim. It retires the release's
stated risk. Confirmed against the prod database on 2026-09-22. Write-up in
[`docs/features/enrollment.md`](docs/features/enrollment.md) → *E2E on real hardware*.

**Decided: prove the release on the new board rather than wait for the stuck one to
recover**. `R0-test-2` waited on `S0-fw-3` since we wrote it, on the
reasoning that the only board on hand could not get through RF calibration to enroll.
That framing tied a release gate to one board's regulator. Using a second board
**dissolves the dependency instead of meeting it**. The dependency was never actually
part of the acceptance criterion — "flash → enroll → online" says nothing about *which*
board.

**Consequences, in order of how much they change**.

1. **`S0-fw-3` is no longer release-blocking, and stays open at P1 anyway**. It is now a
   recovery defect rather than a gate. Kept open because what it names is a fleet
   property, not this board's: any board that browns out during calibration is
   permanently stuck. The product's pitch is that any device that gets a bad one
   recovers itself. Shipping V1 with that unfixed is a decision to take later, on
   purpose — not something to let lapse. This is because the blocker stopped hurting.
2. **The 2026-09-14 conclusion stays the same — resist the tempting read**. It is natural to
   conclude "our agent survived a cold full RF calibration, so our startup draw is fine
   after all." **It does not follow**. The board that passed is an S3. The brownout work
   is about a classic ESP32-DevKit v1, with different silicon, a different regulator and a
   different supply. Its `device_progress` shows no `brownout` stage at all. The S3
   never ran that experiment. "This supply carries a cold calibration and our startup
   draws more than it needs to" thus still stands, and the single-variable test —
   v0.2.0 onto the stuck DevKit v1 — is still untried in the bench order in
   [`TODO.md`](TODO.md).
3. **R1 has no blockers**. `R1-test-1` depended on `R0-test-2` in practice. An enrollable
   board now exists. R1's other seven tasks landed 2026-09-17, so one bench run closes the
   release. Caveat: `presence_reported = f` and `last_seen` is 2026-09-19. We unplugged the board since then. Thus, it must be re-plugged first.
4. **`S0-test-2` has no blockers too**. It was parked since 2026-09-11 on "no C3, C6 or
   S3 on hand". The board that passed *is* an S3. Thus, the native-USB re-acquire path is now
   testable on the hardware we wrote it for.

**R0 does not close on this**. Its other gate is `S0-test-3`, the unaided onboarding run,
put in front of the release on 2026-09-11 (`docs/roadmap.md`) because R0's risk is
onboarding. This run's operator wrote the flasher, which measures the stack rather than
the on-ramp. R0 closes when someone who did not see the code onboards a board.

## 2026-09-22 — The Arduino library gets its own layout id. Config stays in flash

**R3-fw-1**, a spike. Details in
[`design/decisions/arduino-gets-its-own-layout-id.md`](design/decisions/arduino-gets-its-own-layout-id.md).
Evidence in `spec/open-questions.md` → *ota-library*. Reproduction in
[`docs/runbooks/arduino-partition-measurement.md`](docs/runbooks/arduino-partition-measurement.md).

**Decided: (a), a packaged partition table shipped as a sketch-local `partitions.csv`,
under a new layout id `ab-4m-arduino-v1` — not `ab-4m-v1`, and not NVS**. Same
`ota_slot_size` (1966080), different map: `nvs` 20K@`0x9000`, `otadata`@`0xe000`,
`ota_0`/`ota_1` `0x1E0000` @ `0x10000`/`0x1F0000`, `ff_cfg` 4K@`0x3D0000`,
`coredump`@`0x3F0000`.

**1. The reason is not packaging taste. It is invisible without compiling**. The
Arduino upload and merge recipes hardcode `0xe000` for `boot_app0.bin` and `0x10000` for
the app, *independently of the target flash table*. A sketch-local `ab-4m-v1` builds
green and emits a byte-correct `ab-4m-v1` partition binary — then the upload writes
`boot_app0` across the tail of `nvs` and the app at `0x10000`. This is the second half of
`otadata`, from where it runs on over `phy_init`, `ff_cfg` and the alignment gap. `ota_0`
does not start until `0x20000`. Unbootable, and unfixable by OTA. Stock `min_spiffs` already carries
two 1966080 B slots at the offsets the recipe actually writes. `ab-4m-arduino-v1` is that
map with a 4 KB `ff_cfg` carved out of the SPIFFS region.

**2. NVS lost on provisioning, not on size**. Measured: +7460 B flash for an NVS config
read, +324 B for a partition read, against a 1920 KB slot — the cost argument decides
nothing. What decides it is that the upload writes four offsets and nothing else. Thus, no
credential can reach NVS before first boot. (B) would need a serial handshake or Improv.
And *Erase All Flash Before Sketch Upload* is a one-click IDE menu that wipes NVS with the
device credential in it, leaving a board that must re-enroll with a single-use token that
is already burnt.

**3. A sketch-local file, because the board menu does not exist on every board**.
`platform.txt` resolves the table `build.partitions` < variant < sketch folder, last
winning — checked against the default menu, an explicit `PartitionScheme=min_spiffs`, and
a variant table. **52 of 409 boards have no `PartitionScheme` menu at all**, 35 of them
hardwired to `default`, including `esp32doit-devkit-v1`. "Select this scheme" is advice
that does not exist on their board. A file in the sketch folder works everywhere and
travels across a board change.

**Gotchas banked for the rest of R3.**

- **The IDE's `Maximum is N bytes` is not a layout check**. It reads the board menu's
  `upload.maximum_size`, not the built table — reported `1310720` for a build whose slots
  were `1966080`. `R3-fw-5` must get the layout from what the firmware announces.
- **A library cannot ship the table**. The prebuild hook reads the *sketch* folder. Thus, a
  `partitions.csv` under `libraries/` never takes effect. It has to arrive with the
  example (`R3-fw-4`). The override is silent when it happens.
- **No custom bootloader is necessary**. The core's prebuilt bootloader is already
  `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` with anti-rollback, Secure Boot and flash
  encryption off (exactly `design/partitions.md` §2) and `esp_https_ota.h`,
  `esp_ota_ops.h`, `nvs.h`, `mqtt_client.h` all ship in the core.
- **Everything above is a property of `esp32:esp32@3.3.12`**. The hardcoded `0x10000` is a
  convention, not a contract. Re-run the runbook on every core bump.

**PROPOSED, not applied** (`spec/` has protection during `/implement`): add
`ab-4m-arduino-v1` to `spec/device-protocol.md` and to
`firmware/manifest.py::SUPPORTED_LAYOUTS`. This is a single-entry dict built from two
constants today and becomes a real table with a second layout in the fleet.

**On method**. The spike installed a ~7.8 GB toolchain on a box that runs above 90% disk.
It went into a scratch tree with `arduino-cli`'s data dirs redirected out of `$HOME`,
was pruned to 2.1 GB once the target set was known, and disappeared at the end. A spike
that leaves a toolchain behind is a spike that breaks the next build.

---

## 2026-09-22 — The first CUJ is the maker's journey, and its Driver uses segments

**R3-spec-1**. `spec/cujs.md` now exists. The project has had acceptance criteria written
against journeys nobody wrote down since 2026-09-11. CUJ-1 is that journey — Alex
and a DevKit, from a working sketch to a board in the field that fixes itself.

**1. One CUJ, and it goes through the library rather than the agent**. The tempting first
CUJ was the one R0 already built: flasher page → board online. It was not chosen. The
product's claim is that *your* firmware becomes updatable, and a CUJ describing the
prebuilt agent would described the demo instead of the product. The agent connects,
heartbeats and blinks. Thus, a journey about it is a journey about updating a device that
does nothing its owner cares about (`docs/HOBBYIST.md` §4.1, and the R3 ADR). The agent
path is a legitimate *second* CUJ and appears as a proposal in `spec/open-questions.md`.

**2. The Driver uses segments, against the template**. `spec-cujs-template.md` gives a CUJ
one `Driver:` — a single harness command or scenario id. CUJ-1 crosses R0, R1, R2 and R3,
so a single Driver could only name a harness that does not exist until `R3-test-1`. That
is not merely unsatisfying: `/replan`'s T3 gate reads these Drivers at sprint close. Thus, a
CUJ ungradeable until R3 would kept fleetforge's gate red for every sprint in
between, including ones with no connection to this release. CUJ-1 thus names a
harness **per segment** and says which are gradeable — today, steps 3 and 5 on the agent
path (`just agent-qemu`, `just sim-fleet` + `POST …/deploy`, `pytest tests/test_enroll.py`).
Steps 1–2 and 6 when R3 lands.

**A segment with no harness is not graded and is not a pass**. Written explicitly into the
file, because the obvious way to get this wrong is to grade the half that is easy and call
the CUJ green.

**3. Every harness named passed resolution checks before it appeared**. `just --show`
for each recipe, `pytest --collect-only` for the test module. A spec that cites
`just cuj-1` because it sounds plausible is worse than one that cites nothing. It makes
T3 fail for a reason that has nothing to do with the product.

**4. Success Criteria cite numbers, they do not invent them**. 5 min for a healthy deploy,
2 s for the dashboard, Fleet safety 100% — all from `prd.md` → *Requirements & targets*.
`spec/` disagreeing with itself about a target is the failure mode `prd.md`'s own header
warns about.

**Gotcha: this task edited `spec/`, which `CRITICAL.md` forbids `/implement` from doing**.
The exception is narrow and worth stating. This is because the next `spec`-category task will hit
it too: a filed, prioritized `spec` task whose deliverable *is* a spec file is the
sanctioned route for changing `spec/`. It still carries the CRITICAL escalation —
strongest model, and a mandatory review of the diff before commit. Scope was held to one
new file, two additive `Supported By` blocks and one deleted stale paragraph. We did not reword any requirement, target or acceptance criterion.

Details and T2 evidence: `docs/features/ota-library.md` → *The project's first written CUJ*.

---

## 2026-09-22 — The thin OTA library becomes R3, and the v1 ladder shifts by one

**`/new-feature`, from [docs/HOBBYIST.md](docs/HOBBYIST.md) §4.1**. `prd.md` has always
promised two device-side deliverables — a prebuilt agent *and* a thin OTA library to
embed in custom firmware. Only the agent was ever planned. The library now has a feature
file, requirements and a release.

**It ships after auto-rollback, not before**. A library is a multiplier on but safe
deploy currently is, and today's Deploy button has no checksum, no device-armed confirm
and no rollback until R2. Shipping the four verbs into other people's `setup()`/`loop()`
first would put the unsafe path inside custom firmware on boards chosen. This is because they are
hard to reach. Full reasoning:
[design/decisions/ota-library-ships-after-safe-deploy.md](design/decisions/ota-library-ships-after-safe-deploy.md).

**The renumber**. Library = R3. Health & telemetry R3→R4, self-test R4→R5, signed OTA
R5→R6, V2 R6–R10 → R7–R11. Affordable because **no R3+ task IDs existed** — R0 and R1 are
the only releases with tasks. Thus, nothing archived changed. The 23 forward-looking release
references in `src/`, `tests/` and `agent/sdkconfig.defaults` were swept in the same
change. Comment-only, no config lines touched in the CRITICAL file. **Entries below this
one were not rewritten**. They are accurate about what was true when written. Thus, a
pre-2026-09-22 entry saying "R5" means what is now R6.

**The release opens with a spike**. Where a library user's config lives remains open. The
agent uses the `ff_cfg` flash partition, which an Arduino IDE build does not have. OTA cannot add a partition. Recorded in
[spec/open-questions.md](spec/open-questions.md). `R3-fw-1` measures what an Arduino build
actually does to the table before anything else is estimated.

**`spec/cujs.md` gets written here too.** *Unaided onboarding* has had acceptance criteria
with no journey to hang from since 2026-09-11. "I have a sketch and a DevKit on the desk"
is that journey. It is this release's subject.

---

## 2026-09-17 — Deploy state is a column of the fleet read model, not a feature with its own wiring

**R1-fe-1**. The dashboard can now deploy a version to one board and watch it land. Almost
every decision here was about what *not* to add.

**1. `deploy` rides on `GET /v1/devices`**. No `/v1/devices/{id}/deploy/state`, no second
poll, no SSE payload to parse — the same shape `arrivals` already uses, for the same
reason. "The event is a hint. `GET /v1/devices` is the record" is the one refresh engine
this app has. A second one would have to re-derive when to fire, when to wait and
what to do with a dead session. It also makes the page honest about a board that stopped
reporting. A sleepy board publishes nothing for an hour and its last known deploy state is
still on screen. This is because the row comes from the database rather than assembled from
frames that arrived while the tab was open. A reload, a second tab, and a colleague's
browser all show the same thing for free.

**2. The read lives in `deploys.py`, beside the writer**. `deploy_events` has exactly one
module that touches it. The invariant `tests/test_invariants.py` enforces for writes. The same logic applies to reads. `DISTINCT ON (device_id) … ORDER BY at DESC, id DESC` is
where "which transaction is current" is *defined*. The router is not the place to
define it. `devices.py` calls `latest_deploys()` the way it already calls
`progress.latest_progress()`. The `id` tie-break is load-bearing, not tidiness: the
`requested` row and the board's first status share a millisecond in practice.

**3. `pct` is text, never a bar**. `deploy_events` is a transition log and the writer keeps
the *first* `pct` per `(device, cmd, state)`, so our agent contributes one number for an
entire download. A progress bar driven by that sits at one value for two minutes and reads
as a hang. This is the exact failure this task existed to delete. There is no
`role="progressbar"` anywhere in the cell, and a test asserts it.

**4. `awaiting_safe_window` gets a sentence, not a spinner**. "waiting for a safe moment —
the board decides when, and can wait indefinitely". It is not styled `bad`, there is no
`aria-busy`, and nothing on either side expires it: the device owns the reboot
(`design/architecture.md` principle 5) and a client-side deadline would be this dashboard
inventing a policy the system does not have. T2 left a board parked for five minutes and
confirmed the label stays the same and `SELECT count(*) … WHERE is_terminal` is still 0.
More generally the cell never switches exhaustively on `state`: `deploy_events.state` is
TEXT with no CHECK. Thus, an unknown state renders as itself, and `is_terminal` is only ever
the server's answer — a second definition of "terminal" in TypeScript is the failure
`deploys.py`'s header warns about.

**5. An "fe" task shipped two read endpoints on purpose**. `GET /v1/artifact` and
`DeviceSummary.deploy` are both backend, and splitting them into an R1-be-5 would cost
a plan, a branch and a review to add ~60 lines that only this screen consumes — while
leaving the frontend task unshippable until it landed. The rule this follows: a read model
belongs to the screen that needs it. The list endpoint went on the **admin** router. Note
that `/v1/artifact` is *also* a public prefix (`artifact_download.py`, where the signature
is the authorization). Thus, the 401 (including with a `?exp=&sig=` on it) has an assertion in
both T1 and T2.

`spec/device-protocol.md` does not say that `up/status.state` is an open vocabulary the
dashboard renders verbatim, though the server's TEXT column and advisory `DeployState`
already assume it. A proposal only. `spec/` has protection.

---

## 2026-09-17 — `fw_version` is the version that BOOTED, and there is now exactly one way to say it

**R1-fw-2**. The behavior was already right. Nothing kept it right.

**1. The fix was a seam, not a behavior change**. `esp_app_get_description()` already
returned the *running* image's descriptor (the new slot's after an OTA, the old slot's
again after a rollback) but two call sites read it and a third plausible source existed:
`ff_ota_cmd_t::version`, the version the server asked us to install. Reporting that is
correct on every deploy that worked and wrong on every deploy that did not, that is,silent
exactly when the fleet needs the field. So `ff_identity_fw_version()` is now the only way a
version reaches the wire, and three tripwires in `tests/test_ff_cfg.py` pin it: one source,
both payloads, and `ff_ota.c` neither emitting `"fw_version"` nor including `ff_identity`.
Tripwires rather than a runtime check because no host test can execute this code and the
fix would ship by OTA to a board whose OTA reporting is what is broken.

**2. The boot line now names the image state**. `running image: fw_version 0.3.2, ota state
pending_verify` is what distinguishes an applied update from a rolled-back one in a serial
log with no server attached. It allows reads only (`esp_ota_get_state_partition()` and nothing
else) and deliberately **not** merged with `ff_mqtt.c`'s reader of the same otadata: two
small readers of one state is the cheap outcome, one shared helper that someone later
improves is a bricked fleet.

**3. No cross-reboot state**. Still nothing persisted — the announce from the image that
booted is the whole report. Restates R1-fw-1 §1. Supersedes nothing.

**4. T2 ran the negative before the positive**. This is because the negative needs the board still
on the old image and is the half nobody checks: told `0.3.2`, failed on a corrupted digest,
and `up/hb` plus `devices.fw_version` still read `0.3.1` afterwards. Only then the applied
update, `0.3.1 → 0.3.2`.

**5. `apply: "on_command"` is how you drive a QEMU acceptance run**, not a race against
`esp_restart()`. R1-fw-1's workaround (poll for `staged and bootable`, kill before the
reboot) has a **40 ms** window and loses: the board soft-resets, hits the emulator's known
`esp_timer_impl_init` panic. The bootloader retires the `PENDING_VERIFY` image exactly
as designed — leaving the old slot running and `otadata` `aborted`, which reads like a
firmware fault and is not one. Staging without applying and then power-cycling deletes the
race instead of trying to win it (`docs/runbooks/agent-qemu.md`).

`agent/version.txt` → `0.3.1`. `spec/device-protocol.md`'s example `agent_version` string
is now stale — a proposal only. `spec/` has protection.

---

## 2026-09-17 — The agent applies an update: checked against flash, undone on mismatch, and finished at `rebooting`

**R1-fw-1**. `ff_ota.c` is the first code in this product that moves a boot partition. Thus,
the decisions are mostly about what it refuses to do.

**1. R1 stops at `rebooting`, and nothing is persisted across the reboot**. The agent
publishes `staging → downloading → verifying → staged → applying → rebooting` and calls
`esp_restart()`. No `confirming`/`confirmed`, no `cmd_id` in NVS: the image that comes back
announces itself and that announce is the whole report. Half a cross-reboot state machine
— a stored `cmd_id` nobody drives to a terminal state — is worse than none. R2-fw-3 owns
the confirm timer as a shipped feature. The confirm/rollback pair that was dormant in
`ff_mqtt.c` since R0 goes **live** as a consequence of this task and was deliberately left
untouched (checked in T2: the OTA'd image logged `CONFIRMED` after its announce PUBACK).

**2. The sha256 is taken by reading the partition back, after `esp_https_ota_finish()`,
and a mismatch restores the boot partition**. Hashing the stream as it arrives is the
obvious design and it is wrong twice over: `esp_ota_write` withholds the image header's
first 16 bytes until the write completes. Thus, the stream hash covers bytes that were never
on flash. It cannot detect a bad flash write. This is the failure that matters. The
undo (`esp_ota_set_boot_partition(esp_ota_get_running_partition())`) is mandatory because
`finish()` has *already* switched the boot pointer by the time we can hash: without it a
board with a rejected image boots into it at the next power cut, and for this product that
is a van and a screwdriver. Proven in T2 with a hand-published `stage` carrying a corrupted
digest: `boot partition put back to ota_1`, `failed`/`sha256 mismatch`, and a cold restart
still on the old image.

**3. The OTA runs on its own task**. A QoS-1 publish from the esp-mqtt event handler
deadlocks the client, and a multi-minute download inside the handler stops the keepalive.
One task, `s_running` as a flag rather than a mutex: a second `stage` while one runs is
**refused and reported** `failed` on the new `cmd_id`, never queued — two writers to one
slot corrupt it, and a deploy silently waiting behind another is a deploy the server cannot
explain. Duplicate `dn/cmd` ids the pre-existing drops them dedup, not by this task.

**4. `CONFIG_ESP_HTTPS_OTA_ALLOW_HTTP` was NOT added** (the plan called for it, this is the
deviation). Read in the pinned image's sources: `esp_https_ota` gates on
`is_server_verification_enabled()`. This is true whenever `crt_bundle_attach` is set — and
we set it, for the same Mozilla bundle `ff_enroll.c` uses. A plaintext `http://` URL never
reaches the TLS layer, so the option is inert for us. It is also a line in
`agent/sdkconfig.defaults`, a CRITICAL flash-time file, that would relaxed a security
posture to no effect. The QEMU lab downloads over plain HTTP today with the option absent.

**5. The agent resolves the artifact link's 307 itself**. IDF v5.5.5 rebuilds the `Host`
header wrong on a redirect — `esp_http_client_init()` uses `_get_host_header(host, port)`.
Meanwhile, `esp_http_client_set_url()` sets the bare host and only when the host *string*
changed — and an S3-compatible presigned URL signs `host`. Redirect a board from
`:8080` to an object store on `:9000` and it presents a Host that was never signed:
**403 SignatureDoesNotMatch**, reported by IDF as "File not found(403)". So
`resolve_artifact_url()` does one header-only `GET` with `disable_auto_redirect`, captures
`Location` from `HTTP_EVENT_ON_HEADER`, and hands the final URL to `esp_https_ota_begin()`.
Production (GCS on :443) never hit this. A self-hosted MinIO (V2's entire shape) fails
every deploy. The cost is one extra round trip whose body is empty anyway.

**6. The board announces `capabilities: ["ota"]`,** because `POST /v1/devices/{id}/deploy`
answers 409 without it. It stays as short as the truth. It was `[]` at R0 for the same
reason.

**7. `confirm_timeout_s` parses and ignores at R1** (a WARNING if it differs from the
firmware's own `CONFIRM_TIMEOUT_S`). `artifact.sig` parses and ignores:
`spec/device-protocol.md` lists it, `broker/commands.py::stage_payload()` never emits it.
**Spec proposal, not applied** (`spec/` has protection): either the spec drops `sig` or R2
implements it.

**8. `apply: "on_command"` stages and stops at `staged`,** not at `awaiting_safe_window`.
An always-on agent has no window to wait for, so reporting one would be a state nothing
ever leaves. R1 ships no `apply` command. Thus, the board simply waits where the simulator
waits.

**Known limitation of the lab, not of the firmware:** QEMU panics on the boot that follows
`esp_restart()`, inside IDF's `esp_timer_impl_init → esp_intr_alloc`, before `app_main`,
in whichever image it lands on — including the pre-OTA `0.3.0` that boots fine from
power-on. A peripheral interrupt survives the soft reset that the CPU does not. The OTA'd
image was proven to boot, announce `0.3.1` and confirm itself by cold-starting the
emulator instead. `docs/runbooks/agent-qemu.md` has the decoded backtrace and the recipe.

Details: `docs/features/ota-deploy.md` → *The device half (R1-fw-1)*.
`.claude/plans/R1-fw-1-esp-https-ota-update-command.md`.

---

## 2026-09-17 — Every device-reported deploy state is a row, recorded once, and the server still authors no cancel

**R1-be-4**. `up/status` now writes `deploy_events` through the table's one writer,
`deploys.record_observed_status`. Five decisions.

**1. The retained topic forces deduplication on `(device_id, cmd_id, state)`**.
`up/status` carries the retain flag and the ingestor re-`subscribe`s on every connect. Thus, a broker
blip, a container restart or a stack deploy replays the last status of every board.
Retained status is nevertheless **ingested** (unlike telemetry and log, which the ingestor drops when retained). This is because the retained value is exactly how an outcome published
while the ingestor was down is delivered at all. There is no manual ack. A dropped
status is an outcome lost forever. So the duplicate is handled in the writer, not by
dropping the message. It is a **writer rule, not a unique index**. A repeated state is
legal data (`downloading → failed → downloading` inside one retry). Thus, the database must
still accept it. The deliberate consequence is that a repeated state inside one
transaction collapses to its first occurrence and the stored `pct` is the first one seen
. `deploy_events` is a log of transitions, not a progress feed.

**2. A replay proves nothing about liveness**. A retained status records its state but
does not move `last_seen`. Thus, an ingestor restart cannot mark a dead fleet alive. It
still resolves the device through `store.fetch_live_device` (the old private `_fetch`,
now public), which keeps **one** drop path for the unregistered/decommissioned case.
`deploy_events.device_id` is an FK with `ON DELETE RESTRICT`, and an insert for an
unknown device would raise `IntegrityError` that the message loop swallows as "ingest
failed", losing the write.

**3. The wire can not author `requested`. An unmapped `cmd_id` goes into the record anyway**.
A board reporting the server's own state is a firmware bug or a forgery: WARNING, no
row. Conversely a `cmd_id` with no `requested` row — a transaction from before a
database rebuild, a command from another server — **is** recorded, with both version
columns NULL and an INFO line. "Every outcome" means every outcome, including the ones
we cannot explain. Everything else copies from the `requested` row. This is the whole
reason R1-be-2 invented that state: without `artifact_version` on the terminal row, R5's
delivery-success KPI is uncomputable.

**4. There is no server-authored cancel, and TODO's "cancel" is the device's rollback**.
R1 ships no `cancel` command. `broker/commands.py` publishes `stage` only. So: success
→ `confirmed`. Failure → device-reported `failed` (plus the server's `publish_failed`).
Cancel/abandon → the device's own `rolled_back` or `failed`, because the device owns the
reboot and the rollback. When a new deploy supersedes an in-flight one the server writes
**nothing**. That would be authoring a state for a command the device provably did
receive, which the 2026-09-17 R1-be-2 entry forbids. The superseded intent stays an open
transaction with no terminal event. This is precisely what "fleet safety loss" means
in R5, and is the truthful record. Do not "fix" this.

**5. Device-reported `detail` undergoes sanitization before it persists forever**. Control
characters stripped (a device that can inject a newline can forge a log line — the
`api/schemas.py::_printable_detail` precedent), truncated to 200 chars
(`MAX_PROGRESS_DETAIL`, restated locally because `deploys.py` is transport-agnostic and
must not import the FastAPI side), and `https?://\S+` redacted to `<url>`. The signed
download URL is a bearer credential, our agent does not echo it but a third party's
can, and R1-be-3's evidence asserts no stored `detail` contains `http`. Kept true by
construction. The wire model coerces rather than raises (`pct` that is not an int in
0..100 → dropped, a non-string `detail` → `str(...)`): a `ValidationError` on a
*retained* topic loses the same outcome on every reconnect.

`EventType.DEVICE_DEPLOY` is emitted only when a row was actually written (a deduped
replay is not news) and the SSE envelope gains no field: the consumer re-reads.

---

## 2026-09-17 — Firmware downloads go through our own signed URL, and the API redirects rather than proxies

**R1-be-3**. The device receives a link on **our** origin (`GET /v1/artifact/{sha256}/bin?exp=…&sig=…`, the shape `spec/device-protocol.md` already
fixed) instead of the object store's presigned URL. Six decisions.

**1. The signature is the authorization. It is ours**. One HMAC-SHA256 over
`v1\n<sha256>\n<exp>`, keyed by `ARTIFACT_URL_SECRET`, minted in
`api/routers/deploys.py` and checked in `api/routers/artifact_download.py`
(`src/fleetforge/artifact_urls.py` is both). This is the **second unauthenticated
endpoint** after `POST /v1/enroll`. A board has no admin token and never will. What it
buys over issue the store's URL: the URL shape and the authorization are backend-
independent (S3 today, GCS in prod, neither visible to the board), the store URL's short
life is decoupled from the command's life, and rotating the secret kills every link in
flight — at most `SIGNED_URL_TTL_S` of staged deploys, which simply re-deploy. There is
deliberately **no key rollover**. One key, one rotation story.

*The three rules a reviewer must check, each a way this is normally wrong:*
`hmac.compare_digest`, never `==`. The handler checks the MAC **before** `exp` and **before**
any store call, so "expired" versus "forged" is not an oracle and an anonymous caller cannot
drive an IAM `signBlob` call. The digest is **rejected, never normalized**. `exp` has
exactly one spelling (digits, no leading zero, no sign, no underscores — `int()` accepts
three of those four).

**2. 307 redirect, not a proxy**. `design/production.md` promises artifacts are "served
without touching the API process". A proxy would need an HTTP client in the production
image (`httpx` is a dev dependency) and would hold a uvicorn threadpool slot per board
for a 1.9 MB transfer on a 256 M container. It also means `Range`, `Content-Range`,
suffix ranges and 416 are the **store's** RFC-correct implementation rather than a
hand-rolled parser on the OTA critical path — which matters because R5 resume is exactly
a `Range:` request. Checked for real against MinIO in
`tests/test_artifact_download_minio.py`.

**3. One upstream signature per artifact per cache lifetime**. `storage/urlcache.py`
(`SignedUrlCache`, modeled on `CatalogCache`) is now the **only** module in the app that
calls `ObjectStore.signed_url`. A tripwire in `tests/test_api_deploy.py` holds it there.
A URL stays in use only while `monotonic() < signed_at + ttl - ARTIFACT_URL_REFRESH_MARGIN_S`
(default 300 s). Thus, no board is ever handed a link that dies mid-transfer. The cache never stores failures. Measured: ten ranged downloads of one artifact cost one signing call.

**4. The download path touches no database**. A download keeps working during Postgres degradation. The signature already carries the authorization — membership in `artifacts`
tells a signature-holder nothing new. A validly signed digest with no object behind it
ends as the store's own 404 after the redirect. A malformed digest is **404, never 422**.
A schema-error body is an oracle, and the only useful answer to an unsigned caller is one
uniform "no".

**5. A deploy no longer fails fast when the object store is unreachable — deliberate**.
`POST /v1/devices/{id}/deploy` used to sign through the store and answer 503 when that
failed. It now mints locally and never touches the store, so that check is gone: nothing
in the four-verb seam can test existence cheaply (`get` downloads the whole image), the
`artifacts` row is already the evidence the bytes were stored, and the download endpoint
answers the outage honestly at the moment it is true. In exchange the deploy path gained
a **503 when `ARTIFACT_URL_SECRET` or `PUBLIC_BASE_URL` is unset** — a 202 carrying a URL
no board can redeem is the lie `NullCommandPublisher` refuses to tell.

**6. Residual, recorded rather than fixed: uvicorn's access log prints the signature**.
The application never logs a URL, a signature or the secret (asserted in three suites).
But the access line contains the full request target, so anyone who can read container
logs can replay a link for its remaining life. Acceptable at v1 (reading the logs
already implies host access, and the link expires) and the fix (a `--access-log`
formatter that strips the query) belongs with the observability work, not here.

**Production prerequisite — filed, not applied**. `services/prod/.env` needs a fresh
`ARTIFACT_URL_SECRET` (32-byte hex, `just artifact-secret`) and
`PUBLIC_BASE_URL=https://bingo.tvaroska.sk`. The prod compose must pass both to the
`api` service, **before the next prod deploy** — otherwise every deploy answers 503.
`services/` is a different repo and root `CLAUDE.md` forbids changing prod env without
asking. Thus, this appears in `docs/features/ota-deploy.md` the way R1-be-2 filed
its `MQTT_COMMAND_*` prerequisite.

---

## 2026-09-17 — The API commands over MQTT as its own credential, and a retry is the same transaction

**R1-be-2**. Four decisions, all about who can say what and what gets written down.

**1. A fourth broker credential, `commander`, that can only send on `dn/`**.
`mosquitto/bootstrap.sh` creates a dynsec role with exactly one ACL (`publishClientSend 'ff/v1/d/+/dn/#' allow`) and one client holding it. Not the dynsec
admin, which is broker-root over `$CONTROL` and needs none of this to publish a deploy.
Not the ingestor, which must stay read-only. The role has **no** `subscribePattern` and
**no** `publishClientReceive`. Thus, a leaked deploy credential cannot forge `up/status` or
read the fleet. The ingestor stays the only subscriber. The `device` role is still
empty. The fleet's authz is the two `%u` pattern rules in `mosquitto/acl`, and adding a
`+` rule to `device` to "make commands work" would be a confidentiality breach. It does
work because **both ACL backends run and allow wins**: the pattern file's
`pattern read ff/v1/d/%u/dn/#` grants delivery over dynsec's default receive-deny. That
is now asserted live by `just broker-check` (`_check_command_delivery`: A receives, B
does not, and the commander drops on `up/`).

*Gotcha, and it cost the most time to learn in R0:* MQTT 3.1.1 has **no deny feedback**.
A refused publish looks exactly like a delivered one — no PUBACK reason code, no error.
Every negative broker assertion has to be "nothing arrived at a subscriber", never "the
publish raised". Reason codes exist only over MQTT 5 from inside the container.

**2. `requested` is a server-authored state, and the only one**. It records *"we
published a command"*, which no device can report. Without it an abandoned deploy is
invisible to the KPIs and R1-be-4 cannot map an incoming `cmd_id` to the version that was
intended. The single exception is the one **terminal** state the server can write:
`failed` with `detail={"reason": "publish_failed"}`, honest. This is because the broker refused. Thus,
the board provably never saw the command and the transaction the `requested` row opened is complete rather than left open forever. Beyond that the server never writes a state for a
transaction the device did receive, and **never expires `awaiting_safe_window`**. A
vehicle in motion can sit there indefinitely (`design/architecture.md` principle 5). There
is no sweeper, no timeout task and no `asyncio.sleep` on this path, and
`tests/test_api_deploy.py` asserts their absence in the source.

**3. A retry inside the URL's TTL is the same transaction, and writes no second row**.
The device deduplicates on the command `id`. Thus, a retried `stage` must carry the id the
first attempt used or the board downloads the same firmware twice. A POST for
`(device_id, sha256)` matching the newest `requested` row for that device (younger than
`SIGNED_URL_TTL_S` and with no terminal event) reuses that row's `cmd_id`, signs a
**fresh** URL, republishes, and answers `reused: true`. A different artifact is always a
new intent. The TTL bound is what makes it safe: past it the first URL expired. Thus, a
board that never acted on the first command cannot act on it now.

**4. The signed URL is a bearer credential and never touches disk**. It is not in the 202
body, not in `deploy_events.detail` (which carries only sha256/size/target/apply), and not
in any log line. The publisher logs `id` and `type` only. The adapter signs one URL per accepted
deploy. Signing is an IAM round trip on GCS since S0-infra-5. Thus, it is not free.

`deploy_events` now has exactly one writer, `src/fleetforge/deploys.py`, enforced by a
tripwire in `tests/test_invariants.py`. R1-be-4's `up/status` ingestion adds its writer
there rather than growing SQL in `ingestor/handlers.py`.

**Production prerequisite:** `services/prod/.env` needs `MQTT_COMMAND_USERNAME` /
`MQTT_COMMAND_PASSWORD` before the next prod deploy, or `mosquitto-init` refuses to start
(`:?` on both) and every `POST /v1/devices/{id}/deploy` answers 503. `services/` is a
different repo and prod env never changes without asking. Thus, this lives as a filed item, not done.

Detail and T2 evidence: `docs/features/ota-deploy.md` → *Deploy orchestration (R1-be-2)*.

---

## 2026-09-16 — Version labels live in their own table, and the artifact size cap is one number

**R1-be-1**. Three decisions, all forced by things that were already frozen.

**1. `artifact_versions`, not a `version` column on `artifacts`**. S0-infra-4 froze
`artifacts` content-addressed (`sha256` is the primary key) and left no `version`
column, while `spec/device-protocol.md` and `spec/prd.md` → *Retention* both need one. A
column could not work: one digest would carry exactly one label, so re-tagging
byte-identical firmware would be a PK collision rather than the ordinary thing it is.
Migration `0004` adds a small mutable label layer over the immutable blobs —
`(target, version)` PK, a real FK to `artifacts.sha256` with `ON DELETE RESTRICT`, and an
index on `(target, created_at)`. Same shape as S0-infra-6's index-as-pointer decision.
`artifacts` stays the same.

*Gotcha for R2's pruner:* `RESTRICT` means the label must be deleted before the blob. That
is the intended order (it is what stops the pruner deleting bytes a release still names)
but a pruner written blob-first will simply fail.

*Gotcha for the next migration:* unlike `0003`, `0004` carries a foreign key. `0003`'s
absence of FKs was a hard constraint (PostgreSQL cannot FK into the JSONB `builds.outputs`), not a
project-wide principle. Use one where the column is plain.

**2. Three statuses, because a content-addressed store collapses two success cases**. New
label → 201. Same bytes under the same `(target, version)` → 200 `created: false`, since a
re-`put` of the same key is a no-op by construction. Same label over *different* bytes →
409, label unchanged: a version is a promise about which image it is, and silently
re-pointing it would make every `deploy_events` row that mentions it ambiguous. Re-tagging
the same bytes under a second label is free — both labels name one object.

**3. `prd.md`'s "1.9 MB" and `ota_slot_size` 1966080 are one number, not two**. 1966080 B
is 1.875 MiB, which rounds to 1.9 MB. The task as filed asked for two rejections with two
error messages. Implementing that would produced a second, slightly different cap and
a rejection nobody could explain. The endpoint enforces the authoritative one only —
`firmware/manifest.py::SUPPORTED_LAYOUTS`, already what agent-bundle validation reads. Thus,
an upload and a bundle cannot disagree about how big a slot is. `spec/` has protection during
`/implement`. Thus, the clarification is **proposed** in `spec/open-questions.md`, not applied.

Detail and T2 evidence: `docs/features/ota-deploy.md` → *Artifact upload (R1-be-1)*.

---

## 2026-09-16 — R1 opens while R0 stays open, because R0 is parked and not in progress

`TODO.md` carries Sprint 0 plus **one** release, and from today it carries two. That is a
deliberate exception, not drift. Thus, it appears rather than left for the next
`/replan` to discover.

- **The rule assumes the active release is being worked on**. R0 is not. Every desk-bound
  task in it is complete and archived. All five open tasks need a physical board, and the
  gating one (`S0-fw-3`) needs the board *and* a bench session. "One active release" is a
  focus rule. There is nothing left to focus on — the alternative to opening R1 is not
  finishing R0 sooner, it is not building anything until hardware appears.
- **R1 lost its blockers the day before**. `R1-BE-0` closed on 2026-09-15: the GCS credential
  is an impersonation, `signBlob` measures rather than assumed, and containment undergoes a check through the adapter. R1-be-3's signed-URL delivery thus rests on a
  mechanism that was round-tripped. This is what made R1 startable at all.
- **Seven of R1's eight tasks need no board, and that includes the firmware half**. The
  non-obvious part: `docs/runbooks/agent-qemu.md` boots the real unmodified
  `agent/dist/esp32` bundle against the dev stack over the emulated `openeth` NIC, so
  `stage → download → apply → reboot → report-version` is exercisable at a desk. R1's
  firmware work is not hardware work.
- **`R1-test-1` stays bench-gated and keeps its name**. The tempting move is to redefine
  the E2E as "passes in QEMU" and close the release. Refused: QEMU has no radio, no power
  behavior and no chip revision, and R1's claim is about a board. The same reasoning that
  keeps `R0-test-2` open keeps this one honest.

**What this obliges**. R0 does not lose priority. The bench session runs the moment a
board is in hand, in the order `TODO.md` gives. `R0-test-2` still closes R0 before R1
can close. When R0 does close, `/replan` archives both R0's and R1's completed tasks and
the file returns to one release. Until then, two release sections coexist and the R0 one
is the one that gets picked up first when hardware exists.

---

## 2026-09-15 — the agent catalog is a pointer object, not a bucket listing (S0-infra-6)

**Closes** the 2026-09-11 entry *agent bundles are artifacts, not image contents*, whose
blocker the S0-infra-5 entry above deleted. The application image now ships **zero**
firmware: `COPY agent/dist /app/agent` and `AGENT_IMAGES_DIR` are gone, `just agent-publish
<target>` uploads a checked bundle as content-addressed blobs. The flasher reads it
back through `ObjectStore`. A firmware fix now reaches boards with no app-image rebuild and
**no restart** — measured at the TTL, same container id.

- **`agent/index.json` is a pointer, not a listing. This is because `ObjectStore` has four verbs
  and `list` is not one of them**. Adding a fifth verb to serve this triggered a rejection twice
  over: listing is a per-backend paging contract, and a catalog defined as "whatever is in
  the prefix" cannot be rolled back, cannot be published atomically, and answers "what is
  current?" with a guess over lexicographic order. The index is the single mutable key in
  the scheme (`Cache-Control: no-store`, never through `put_blob`). Everything else is
  immutable `blobs/sha256/<digest>`. Publish writes blobs first and the index last. Thus, a
  crash leaves unreferenced blobs rather than a catalog pointing at bytes that do not
  exist. **Rollback is thus one index write** (`just agent-rollback <target> <digest>`)
  against a capped 20-entry `superseded` history — not a rebuild. This is what S0-fw-3 and
  S0-infra-2's three stale bundles each cost.
- **No database table for the catalog**. Agent bundles are per-deployment facts, not
  per-tenant records. A row must stay in step with the bytes by hand. The
  index already is that state. `firmware_builds`/`firmware_artifacts` stay empty here.
- **A failing refresh never serves the previous snapshot**. `CatalogCache` clears before it
  reads. Thus, a store outage is a named 503 rather than a manifest whose parts the API can no
  longer issue. The cache never stores failures either. Three distinct answers, and the 503 text
  copies verbatim into the flasher banner. Thus, it carries no bucket, key or traceback:
  *"no agent images exist yet"*, *"the agent image store is unreachable…"*,
  and 502 for bytes that do not match the manifest. `create_app()` does no store I/O. The
  container still starts when the bucket is down.
- **Verification moved forward, it did not move away**. The old startup loader became
  `firmware/bundledir.py` and now **raises** instead of dropping: a publisher that skipped a
  corrupt bundle would report success and leave the flasher serving the previous build.
  Dropping-with-a-warning is still correct on the read side, where one bad manifest must not
  take the other three targets down.
- **The index read-change-write race is knowingly accepted**. Two concurrent publishes of
  different targets can lose one entry. There is one publisher (an operator at a terminal),
  re-running fixes the loser one command. A compare-and-set would need a
  generation precondition the seam deliberately does not expose. Revisit if publishing is
  ever automated in CI.
- **Prod appears as a proposal, not applied**. `services/prod/docker-compose.yml` still sets
  `AGENT_IMAGES_DIR` and carries the now-false "no object store on purpose" comment. The
  replacement (`OBJECT_STORE_BACKEND: gcs` + bucket/prefix/impersonation) is written out in
  [docs/features/infrastructure.md](docs/features/infrastructure.md) → *Production hand-off*
  and root `CLAUDE.md` requires asking before touching production config. **Ordering is
  load-bearing: publish the bundles to GCS before deploying an image that no longer carries
  them**, or prod's flasher answers 503 in between.

Details: [docs/features/infrastructure.md](docs/features/infrastructure.md) → *Agent bundles
come from the store*, [docs/runbooks/agent-build.md](docs/runbooks/agent-build.md),
[docs/runbooks/artifact-storage.md](docs/runbooks/artifact-storage.md).

## 2026-09-15 — the GCS credential is an impersonation, and signing is no longer local (S0-infra-5)

**Completes** the 2026-09-11 entry *agent bundles are artifacts, not image contents*, whose
accepted cost was "onboarding comes to depend on a store that today cannot be
credentialled", and the amendment to
`design/decisions/infrastructure-agent-bundles-are-artifacts.md`: *"the real prerequisite
is not an org-policy exemption. It is that `storage/factory.py` learns to accept a
credential that is not a key file."* It has. `gs://btvaroska` has now been round-tripped
end to end (put, get, a V4 signed URL redeemed with no credentials, delete) with **no
private key anywhere in the process**.

**Containment is the primary reason for it, not signing**. The old docstrings refused ADC
because ADC carries no private key. True, and the weaker argument. Prod's attached identity
is `mainsite@sites-470716`, the estate's shared VM account, and the bucket policy (read
2026-09-15, not inferred from a listing) grants it `roles/storage.objectAdmin` on the
**whole** of `gs://btvaroska`, unconditionally — including this estate's `secrets/` `.env`
backups. Plain ADC would make the `fleetforge-prefix-only` condition on
`fleetforge-artifacts` decorative. Impersonation would be the right answer even with an
org-policy exemption in hand, so **plain ADC still triggers a refusal, and neither credential set
is still an error** — never a revert. Both credentials set is `ObjectStoreConfigError:
… mutually exclusive …`, the same "ambiguous configuration triggers a refusal, not resolved" rule
`factory.py` already applied to backends, now applied to identities.

**`signed_url` is a network call now. There is exactly one code path**. Under
impersonation `generate_signed_url(version="v4")` POSTs to the IAM `signBlob` endpoint
through an `AuthorizedSession` with a backoff retry loop and no timeout of its own. The
adapter **cannot branch on this**: it receives an opaque `bucket_factory` and has no idea
which credential is behind it. So signing always goes through `asyncio.to_thread` under
`_guard` — one wasted thread hop on a pure-CPU operation with a key file, the difference
between a timeout and a hung API without one. The same argument moved
`self._bucket_factory()` inside the guard in all four verbs: the first call resolves ADC
and mints a token, which on a non-GCP host hangs for seconds. `gcs.py`'s docstring said
*"Local CPU only — no `to_thread`, no I/O"*. That sentence became a lie the moment
impersonation was configurable. It is gone.

**`ObjectStoreError`, never `ObjectStoreConfigError`, from the lazy path — the correction
that would otherwise be a 500**. `objectstore.py` states its contract: a config error is
raised "at construction/selection time, never mid-request-body", and
`api/deps.get_object_store` translates it only around `create_object_store`. So eager,
shape-only checks in `_gcs_store` raise `ObjectStoreConfigError`. Anything discovered when
the credential is first *resolved* (no ADC, a refused token, a `signBlob` 403) raises
`ObjectStoreError`, which every caller already maps to a retriable 503.
`test_gcs_missing_adc_fails_the_verb_as_a_backend_error_naming_adc` asserts the class
explicitly, because the two are siblings and neither `isinstance` check falls out of
`pytest.raises` alone.

**The failure that actually happens is a refused token, and untranslated it says nothing**.
An ADC that resolves but can not impersonate raises `RefreshError` **lazily, at first use**,
which `_guard` reports as `gcs get of … failed: RefreshError` — naming neither the
principal nor the missing role. `_impersonated_bucket` thus refreshes eagerly and
translates, naming the target and `roles/iam.serviceAccountTokenCreator` **and nothing
else**: no token, no ADC path, and not the 403 body, which carries an opaque troubleshooter
id that is fine in a log and not in an exception that can reach a handler. Cost is zero —
the client would minted that token on the very next call. Later refreshes still
surface generically. The first failure is the one an operator debugs.

**`project=None` is deliberate and must not be "cleaned up"**. `storage.Client.__init__`
maps `None` to no project. The default `_marker` sentinel sends google-cloud-storage
looking for a project through ADC and raises when it cannot find one. The bucket is
cross-project and nothing here lists buckets.

**The principal undergoes a check as a service-account email and never normalized**.
`<name>@<project>.iam.gserviceaccount.com`, lowercase. `boris@gmail.com`, a bare name, and
an uppercase spelling are all `ObjectStoreConfigError` — `resolve_key`'s and
`parse_blob_key`'s standing rule, applied to an identity. `.strip()` before the match is
the only fix allowed. The scope requested is `devstorage.read_write` only. Signing needs
no scope at all, it is the *source* credential that needs `cloud-platform`.

**Measured, not assumed**. `just storage-check --backend gcs --blob` against real
`gs://btvaroska`: `SELFTEST OK`, `creds=impersonated(fleetforge-artifacts@…)`, and a URL
carrying `X-Goog-Credential=fleetforge-artifacts@…` that an unauthenticated GET redeems.
With no private key in the process, **that get is the `signBlob` verification**.
Containment measured through our own adapter with `GCS_PREFIX=` empty (in-process
confinement deliberately off, so the IAM condition is what answers) and a `put` to
`secrets/ff-impersonation-probe-<uuid>.bin` failed `Forbidden`. Prod itself was **not**
exercised. `ssh prod` writes triggered a refusal by the sandbox classifier. Prod's identity holds
the same tokenCreator grant. Thus, it must pass, and S0-infra-6 owns running it.
Relevant prior art for the 403 that will eventually appear: root `docs/ops-log.md`
F-2026-08-18-001 (the `boris` podcast feed 500'd once on `signBlob` right after a deploy
and recovered by itself — IAM propagation, already RESOLVED, cited here as evidence only).

**Gotcha worth an hour to someone: on this dev box ADC is a USER, not `devserver@`**.
`gcloud config` shows the active account as `devserver@btvaroska`. But `google.auth.default()` returns a `google.oauth2.credentials.Credentials` —
an `authorized_user` from `gcloud auth application-default login`, because the ADC **file**
wins over the metadata server. That principal has no tokenCreator binding and 403s on
`iam.serviceAccounts.getAccessToken`. `CLOUDSDK_CONFIG=/tmp/empty` for one command takes
the file out of the search path, the metadata server answers with this VM's attached
identity (`devserver@btvaroska`, which *is* a granted member). The selftest passes.
This **corrects** the S0-infra-5 plan's measured claim that the dev box cannot impersonate
at all. It can. That is what made the live verification above possible without prod.

**Not done, on purpose:** `services/prod/.env` and `services/prod/docker-compose.yml` stays the same (root `CLAUDE.md` — never change production config without asking). Thus, the
running container still has no object store and its comment *"the GCS service-account key
has no mint path"* is stale. Wiring it is S0-infra-6's first act. It is four env lines
with nothing mounted. `spec/` was not touched: `spec/device-protocol.md` already says
`artifact.url` is an opaque, short-lived signed URL, and how the server gets a signature
is not wire-visible.

Details: `docs/runbooks/artifact-storage.md` (rewritten — the *BLOCKED* section is now
*resolved*, with the grant recipe, the dev-box gotcha and the signing-is-an-API-call
gotcha), `docs/features/infrastructure.md` → *A GCS credential that is not a key file*,
`docs/features/ota-deploy.md` → R1-BE-0 (landed).

---

## 2026-09-14 — the agent invalidates its own credential. The flasher erases nothing (S0-fw-4)

**Completes** the 2026-09-14 correction entry *"the brownout is ours. The flasher erases
the calibration it went to save"*, which diagnosed the defect and named this remedy.
**Supersedes the implementation half** of 2026-09-13 *"the flasher erases `nvs`, never the
whole chip"*: the reasoning in that entry stands unchanged — a chip-wide erase destroys the
cached RF calibration, and whether to clear credentials is a property of the write plan and
never a boolean on the write call — but the address it acted on was wrong. RF calibration is
in NVS, in IDF's `phy` namespace, not in the `phy_init` partition (which our build leaves
empty: `CONFIG_ESP_PHY_INIT_DATA_IN_PARTITION` is unset, the data compiles into DROM). The
targeted wipe thus destroyed exactly what it went to save, on every flash, for
every board, forever.

After this task it is a property of neither. **The flasher erases nothing at all**.
`frontend/src/flash.ts::nvsWipe` is gone, the `wipeNvs` option and its checkbox are gone, and
`planWrite` now *asserts* that no part lands in `nvs` (`assertLeavesNvsAlone`, ranges rounded
out to the 4 KB sectors esptool actually erases, offset read from the table being written).
That guard is why `partitionTable.ts` survives with no wipe to aim: its purpose inverted from
"find `nvs`" to "prove we are not in it", and the new test that doctors a build so a part
lands at 0x9000 is the test that would caught the original bug.

**Credential invalidation moved into the agent, because only the agent can act on one
namespace**. `ff_store_sync_token()` (`agent/main/ff_store.c`, called from `agent_main.c`
right after `ff_cfg_log`) erases `FF_STORE_NAMESPACE` (`ff`, and nothing else) when the
enrollment token in `ff_cfg` is not the one the stored credential was issued against. `phy`
survives. As a bonus it also covers boards re-flashed in the field with
`agent/tools/ff_cfg.py`, which a browser flasher never reaches.

**A digest, not the token**. The stored key is `tok_fp`: the first 8 bytes of
`sha256(cfg.token)` as 16 lowercase hex characters. The token is already in flash in `ff_cfg`
. It stays there after enrollment and nothing blanks it. This is the property the whole
design rests on — so storing it again is not a new exposure *in principle*, but **a digest is
loggable and a live single-use fleet-join credential is not**, and every diagnostic line in
this change wants to name the thing that changed. sha256 via `mbedtls` (already a REQUIRES,
already linked by esp-tls, so ~0 bytes) rather than a CRC. This would saved nothing and
invited the question. 16 characters is inside NVS's string limits. `tok_fp` is inside its
15-character key limit.

**The four cases:**

| `cfg.token` | stored `tok_fp` | action |
|---|---|---|
| `""` / NULL | anything | nothing. A tokenless config — the QEMU smoke build, a diagnostic flash — must never cost a board its credential. Same refusal `ff_store_matches_api_base` already makes for a hostname change. |
| `T` | `== fp(T)` | nothing. The common case, every boot of a settled board. |
| `T` | `!= fp(T)`, or unreadable | `nvs_erase_all` → write `fp(T)` → one `nvs_commit`, in that order on one handle. Erase first because `nvs_erase_all` takes `tok_fp` with it. One commit makes the pair atomic, so a power cut leaves the board as it was and the next boot retries. A fingerprint we cannot compare is not proof the credential belongs to this token. |
| `T` | absent | **adopt: write `fp(T)`, erase nothing.** |

**Why absent ⇒ adopt and not erase — the decision a future reader will second-guess**.
"Absent fingerprint means this board predates the mechanism, so clear it to be safe" is a
landmine. At R2 an OTA replaces the agent *without* writing a new `ff_cfg`. The first post-OTA
boot of every board in the fleet would find no fingerprint, erase its credential, and try to
re-enroll with the long-spent token still sitting in `ff_cfg` → 409 → `park()`. That is a
fleet-wide brick delivered by an update. The migration cost of adopting instead is one extra
flash for the handful of boards enrolled before today, and the log says so in those words.
`nvs_open(NVS_READWRITE)` creating the namespace is likewise intended and safe: `ff_store_load`
probes `mqtt_pass`, not the namespace. Thus, a namespace holding only `tok_fp` still reports
"nothing stored, enroll".

**One wholesale eraser remains, deliberately:** `agent_main.c::nvs_ready()`'s recovery path
still calls `nvs_flash_erase()` on an NVS that cannot be mounted (`NO_FREE_PAGES`,
`NEW_VERSION_FOUND`). That takes the calibration too. It stays (there is no other way
back from an unmountable NVS) but its comment now says out loud that it is the last one.

**T2, without a bench**. QEMU has no radio and so never writes a `phy` namespace of its own.
Seeding one with a canary before the first boot makes the calibration half provable anyway.
Three boots against `just up`: enroll → same token, `reusing the stored credential`, no HTTP
at all → new token written with the new `just agent-qemu-recfg` (writes `ff_cfg` into an
existing image at the manifest offset, NVS untouched, `--fresh` was the only previous option
and it is the opposite of this test), loud erase, `enroll 200`, both tokens `used` on the
server, device `online`. `nvs_tool.py` then still shows `phy/cal_data = ff-s0-fw-4-canary`
beside the *new* credential. Bench confirmation on real hardware is still owed, jointly with
S0-fw-3.

**Size:** `APP_SIZE_BUDGET_BYTES` ratcheted to the measured byte of a rebuild of all four
targets (esp32 991,776 → 993,696, esp32s3 971,168 → 973,136, esp32c3 1,026,240 → 1,028,336,
esp32c6 1,075,744 → 1,077,840) for the new function and its three log strings. All four
bundles were rebuilt so `agent-check-fresh` stays green. Nothing on the wire changed, so
`spec/` was not edited —
enrollment is the same endpoint, the same payloads and the same single-use rule, and *when* a
device decides to re-enroll has always been device-local and unspecified.

## 2026-09-14 — the firmware catalog is keyed on (target, partition_layout) (S0-infra-7)

`firmware/catalog.py` now indexes bundles by `(target, partition_layout)` rather than
target alone, so two layouts for one chip can coexist. One layout exists today
(`ab-4m-v1`, frozen at R0). The moment a second appears, two bundles for one target
would otherwise collide on the same key and the flasher would have no way to ask for the
right one.

**A bundle directory is `<target>` or `<target>.<layout>`**. The dot-suffixed form is the
reader-side convention for two layouts. `just agent-build` still writes the bare
`<target>` form and stays the same. `.` separates because no chip target and no layout id
contains one (both are `SAFE_SEGMENT`: lowercase alnum and `-`). Thus, the split is
unambiguous. `esp32-ab-4m-v1` would not be. Directory/manifest mismatch (a directory
named `esp32.ab-8m-v1` containing a manifest with `partition_layout: ab-4m-v1`) drops with a warning, the same rule as target/directory mismatch.

**The registry, not a relaxation**. Today `catalog.py` compares
`manifest.partition_layout` and `manifest.ota_slot_size` against two module constants.
The wrong fix is to drop the layout check so "two layouts both load". That would let a
bundle declaring *any* string load. `ota_slot_size` would float free of the layout
id, breaking the three-way contract `DECISIONS.md` 2026-09-09 protects. The right fix is
**`SUPPORTED_LAYOUTS: dict[str, int]`**, mapping every layout id the server understands
to the slot size a bundle claiming it must declare. A bundle cannot claim `ab-4m-v1` with
a 4 MB slot. It is a plain `dict`, not `MappingProxyType`/`frozenset` — tests register a
second layout with `monkeypatch.setitem(SUPPORTED_LAYOUTS, ...)`. This is the only way
to exercise multi-layout behavior without a spec change.

**Absent layout resolves while unique. Ambiguous requests name the layouts**. If `?layout=`
is omitted and exactly one candidate exists, `catalog.bundle(target)` returns it — the R0
case, and the backward-compatibility proof for every existing caller. If more than one
exists, it **raises `AmbiguousBundleError`** naming the layouts, which the download route
maps to 409. A `LookupError`, not an `AgentBundleError`: nothing is wrong with any bundle,
the request is under-specified. The answer is to say so (`spec/standards.md`'s Unaided
onboarding rule) rather than to serve whichever sorted first and flash a board with the
wrong partition table. An unknown layout is a clean 404, same as an unknown target.

**S0-infra-6 hand-off:** the object-store key and the publish index must carry
`(target, partition_layout)`. `AgentBundle.key` is the shape to reuse. Do not re-narrow
it to target alone.

---

## 2026-09-14 — the blob key is store-relative, lowercase-only, and carries its own cache header

S0-infra-4 froze the content-addressed key scheme while **zero objects exist**: the same
change after R1 writes the first artifact is a migration over live bytes in a shared
bucket. **Extends, does not supersede, the *one storage model for every image* entry
below**. Code: `src/fleetforge/storage/blobs.py`, migration `0003_artifacts_and_builds`.

- **The key is store-relative. This is the thing that would otherwise were got
  wrong**. `design/artifacts.md` writes the layout as `fleetforge/blobs/sha256/<hex>`.
  This is the absolute *object* path. The `fleetforge/` half is the store's prefix,
  applied by `resolve_key`. So `blob_key()` returns `blobs/sha256/<hex>` and a
  `fleetforge/`-prefixed key is **refused**. Hardcoding the prefix would wrote
  `fleetforge/fleetforge/blobs/…` in production and left dev (dedicated MinIO bucket, no
  prefix) and prod on two different layouts — invisible to every test anyone would think
  to write, visible only in a bucket listing months later.
  `test_blob_key_is_store_relative` is the guard.
- **Lowercase hex only, rejected and never fixed** — `objectstore.py`'s and
  `identity.py`'s standing rule, applied to the digest. `AB…` and `ab…` would be two
  objects holding one artifact. `parse_blob_key` accepts only `blobs/sha256/` + 64
  lowercase hex: nothing before it, nothing after it, no other algorithm.
  (Implementation note worth keeping: the Python regex anchors with `\Z`, not `$` —
  `$` also matches before a trailing newline, so `^[0-9a-f]{64}$` accepts `"<hex>\n"`.
  The PostgreSQL CHECK writes `$`, where POSIX has no such behavior.)
- **`Cache-Control: public, max-age=31536000, immutable` is object metadata, not prose**.
  `ObjectStore.put` grew a `cache_control` parameter and both adapters send it *only*
  when it is not None. Thus, an ordinary `put` is byte-for-byte the request it always was.
  A header asserted only against a fake bucket is a header nobody has seen on an object,
  so `just storage-check --blob` reads it wait a signed-URL GET and
  `tests/test_object_store_minio.py` does the same against real MinIO.
- **`builds.outputs` is JSONB with no foreign key. That has a price**. A bundle build
  produces four parts. Thus, one `artifact_sha256` column cannot hold the result and a
  per-part row would collide on the cache-key PK. The set moves as a unit. But
  PostgreSQL cannot FK into JSONB, so **a future pruner (R2) must treat `builds.outputs`
  as a GC root** rather than trusting referential integrity to keep a referenced blob
  alive. A `build_outputs` join table is the additive migration the day part-wise
  queries appear. Likewise there is deliberately no `artifacts.storage_key` (the key is
  a pure function of the PK, a stored copy is a second spelling that can disagree) and
  no refcount (nothing decrements it yet, and a refcount with no decrementer is a lie).
- **Both tables land empty with no readers**, the same posture `fleetforge.storage` took
  at R0-be-6. That is what makes `downgrade()` an honest reverse here. It will not
  be true next time.
- **The wire-visible half was proposed, not edited**. `spec/device-protocol.md` already
  hands a device `artifact: {url, sha256, …}` and already says `url` is a short-lived
  signed URL. Thus, the key scheme is not wire-visible and no spec change is necessary. The
  optional clarification carried to review, unedited: *`artifact.sha256` is the
  artifact's identity. The server stores the bytes under that digest and nothing else.
  `artifact.url` is **opaque**. A device must never construct, cache-key on, or parse
  it.*

---

## 2026-09-14 — `build_digest` covers the inputs, not the clock

S0-infra-3 adds `config_sha256` and `build_digest` to every agent bundle manifest. The
part worth recording is what goes *into* `build_digest`. This is because it is the thing a future
reader will second-guess.

**In:** a version tag (`v: 1`), `target`, `agent_version`, `idf_version`, `idf_image`,
`source_commit`, `partition_layout`, `ota_slot_size`, `config_sha256`, and each part's
`name`/`offset`/`size`/`sha256` sorted by name. Canonical JSON
(`sort_keys=True, separators=(",", ":")`), then sha256.

**Out, and this is the decision: `built_at`**. A build id has to answer *"is the bundle
on my bench the one you built?"*. A timestamp inside it makes every rebuild of identical
inputs look like a different build, and the field becomes decoration. Identical inputs →
identical digest is thus a property, not an accident, and
`tests/test_agent_manifest_identity.py` asserts it directly — nothing else would catch a
regression there. `flash_size` and `chip_family` are out too: both are functions of target
and config, already covered.

Two smaller calls made with it:

- **One definition, checked rather than duplicated**. `verify_bundle.py` imports
  `build_identity` from `make_manifest.py` (same directory, `sys.path[0]` resolves it,
  both stdlib-only so they still run inside the ESP-IDF builder image). A second copy of
  the canonical serialisation would drift and the drift would present as a false mismatch
  on a good bundle.
- **Absent identity warns. Malformed identity drops**. A bundle with no `config_sha256` is
  old, not invalid — dropping it would take the flasher offline for a cosmetic reason. A
  bundle with a digest that does not match `^[0-9a-f]{64}$` drops. This is because a corrupt
  digest is one that gets compared and believed. `MANIFEST_SCHEMA` stays 1: both fields are
  additive and optional. Thus, a reader has nothing to switch on. It bumps when one is made
  required, which S0-infra-6 can want once every bundle comes from the object store.

Gotcha for the next person who needs an A/B firmware build: pick a config lever that is
not already set. The planned `CONFIG_ESP_MAIN_TASK_STACK_SIZE=4096` was a no-op waiting to
happen (the repo already sets it to 8192) so the verification moved that existing line
instead. An option set to its current value leaves `sdkconfig.resolved` byte-identical and
proves nothing.

---

## 2026-09-14 — one storage model for every image: content-addressed blobs, manifests as views, builds as a cache

Asked whether growing image count means moving from fixed artifacts to dynamic build
with caching. It does not: **prebuilt-and-cached is the steady state and a build is what
happens on a cache miss**. Onboarding must never wait on a compile. There is always a
pinned known-good bundle set that needs no builder running. Design in
[design/artifacts.md](design/artifacts.md). Tasks S0-infra-3 … S0-infra-7.

**Extends, does not supersede, `design/decisions/infrastructure-agent-bundles-are-artifacts.md`**
(2026-09-11). That ADR decided agent bundles move behind `ObjectStore` and rejected a
baked fallback tier. Both hold. This entry says what the storage underneath looks like
once they get there, and finally files the tasks — the ADR has sat "Accepted, not yet
implemented" for three days with nothing in `TODO.md` pointing at it.

- **Artifacts are content-addressed**: `fleetforge/blobs/sha256/<hex>`, write-once,
  `Cache-Control: immutable`. `objectstore.py::put` already documents overwrite-is-safe
  *because* R1 content-addresses. This makes the key scheme real before R1 writes the
  first object. Dedup is a side effect that pays for itself immediately. A new agent
  version changes `app.bin` and nothing else.
- **Manifests act as generated views, not the storage unit**. A manifest for a given
  (target, layout, version) becomes a query rather than a directory of copied bytes,
  which is the only thing that makes the coming combinatorics tractable: 4+ targets ×
  layouts × retained versions, then V2's repo × ref, then V3's delta images. This are
  indexed by *pairs* of versions and thus quadratic.
- **Per-device data stays out of artifact identity**. `ff_cfg` as a separate part is
  what lets N devices share one artifact. It is the structural advantage over
  ESPHome's compile-per-device. Adopted as a standing constraint on future features.
- **The R9 build-cache key is wrong as specified**. `docs/features/build-pipeline.md`
  says `(repo, ref, toolchain)`. That omits build configuration, so two builds of the
  same ref with different `sdkconfig` collide. Corrected to
  `H(idf_image_digest, target, partition_layout, source_tree_digest, config_digest)`.
  Source *tree*, not commit — a dirty tree must not hit a stale entry.

**What this came out of. It is the same lesson as the entry below**. The manifest
records `agent_version`, `source_commit`, `idf_version` and a digest-pinned `idf_image`,
and still could not answer "which build produced this failing bundle?" —. This is because it carries no digest of the **build configuration**. Establishing that every brownout on
record came from a 160 MHz `-Og` build took three sessions and a `git log` correlation
against a timestamp. Provenance that names the inputs but not the configuration is
provenance that cannot settle an argument. `config_sha256` in the manifest and in the
`S0-fe-7` diagnostic bundle (S0-infra-3) is a few lines and would made it a string
comparison.

Also noted, not yet fixed: `agent_version` (`0.2.0`) and the server release version
(`v0.3.3`) are two schemes sharing one word in the prose. The wire protocol already
keeps `fw_version` and `agent_version` apart. The docs must follow it.

---

## 2026-09-14 — the brownout is ours, and the flasher erases the calibration it went to save

Two corrections, both from one observation. **Supersedes the 2026-09-13 entries
*"reducing TX power does not break the brownout loop"* and *"the flasher erases `nvs`,
never the whole chip"*, and the hardware conclusion in S0-fw-3.**

**The observation**. A *brand-new* ESP32 board (same cable, same port that fail under our
image) flashed with ESPHome, associated to Wi-Fi and ran. A new board has no cached
RF calibration, so ESPHome did the same cold full calibration our image dies in, on
the same rail, and survived it.

- **The supply carries a cold full calibration**. The 2026-09-13 entry named the
  discriminating experiment ("flash the stock Arduino sketch with a full chip erase … if
  it survives, our image draws more than it needs to") and called "the supply is marginal"
  the best-supported reading. That reading is now falsified, and so is the
  bulk-capacitance-across-3V3/GND conclusion it pointed at. **The fault is in our image or
  our build configuration.**
- **Every brownout on record came from a build we no longer ship**. The
  2026-09-13T14:15 bundle is agent `19b0a0b`, which predates `d705652` (`-Os`, 80 MHz, max
  modem sleep, TX-power ladder). So all six failing boots ran at **160 MHz with `-Og`**.
  The entry below concedes 80 MHz was "untested on hardware". What was not noticed is that
  it is untested *against the only failure we have*. Retesting costs one flash.
- **A lead the sdkconfig already contains**. We build with `CONFIG_ESP32_REV_MIN_0=y`
  (IDF's default). In IDF v5.5 `components/esp_hw_support/port/esp32/Kconfig.hw_support`
  the rev-0 option carries `select ESP_BROWNOUT_USE_INTR`, justified inline as *"Brownout
  on Rev 0 is bugged, must use interrupt"* — so our min-revision choice force-enables the
  interrupt-based detector. If the board is rev 1 or 3 and ESPHome builds for a higher
  min revision, the two images use **different brownout mechanisms on the same silicon**,
  which reproduces this symptom with no difference in current draw at all. Unresolved.
  The revision is in the boot banner of bundles already collected.

**The second correction: the flasher preserves a partition that holds nothing**. The
2026-09-13 entry below replaced `eraseAll` with a targeted `nvs` wipe in order to keep
"the `phy_init` partition holding the cached RF calibration". RF calibration is not in
`phy_init`. It is in **NVS**, under IDF's `phy` namespace — exactly the bytes `nvsWipe`
fills with 0xFF. With `CONFIG_ESP_PHY_INIT_DATA_IN_PARTITION` unset (our build) the
`phy_init` partition at `0x11000` is unused entirely, the init data being compiled into
DROM. The error code in every bundle says so: `0x1102` is `ESP_ERR_NVS_NOT_FOUND`.

So the reasoning in that entry survives intact and the implementation does not: erasing the
calibration on every flash *is* the mechanism it identified, and the fix aimed at the wrong
address. **No fleetforge-flashed board can currently retain a calibration**. Filed as
S0-fw-4. The remedy is to stop wiping from the flasher and let the agent erase
`FF_STORE_NAMESPACE` when the `ff_cfg` token fingerprint changes. This is the only place
with namespace granularity. Note this does **not** explain the brand-new board above (nothing was cached either way) so the two defects are independent and both are open.

**Method note, worth more than either finding**. Both errors have the same shape: a
comparison against another toolchain ("a stock sketch runs fine") read as evidence about
*hardware*, when the toolchains also differed in what they erase and how they build.
The comparison is only informative when the other side's configuration is clear. The
mechanical version (diff `agent/dist/<target>/sdkconfig.resolved` against the other
build's `sdkconfig`) was available the whole time and was never run. Do that before
theorising about current draw again. Wider ESPHome review, including what is worth reusing:
`products/docs/esphome-review.md`.

---

## 2026-09-13 — the agent's power and size posture: 80 MHz, `-Os`, max modem sleep, a TX-power retry ladder

Came out of a review prompted by the observation that the agent image looked large for
what it does. The source was not the problem — ~2,070 lines of C excluding comments, all
of it load-bearing. The build configuration was. Four changes, two of which are also
candidate levers for S0-fw-3.

- **`-Os` instead of IDF's default `-Og`**. The connect-only agent was 1,079,520 bytes of
  a 1,966,080-byte OTA slot — 55% full before R2 adds OTA and R5 adds signature
  verification. The optimization level alone was worth 8-9% on every target (esp32
  1,079,520 → 990,544, s3 1,060,800 → 969,824, c3 1,129,856 → 1,024,848, c6 1,181,840 →
  1,074,368). Assertions stay on: `-Os` is independent of them. The agent's
  diagnostic output is the product. The cost is less faithful panic backtraces. This is
  a real cost to the serial console's classifier and ran knowingly.
- **80 MHz CPU, down from 160**. This agent is I/O-bound by construction — DHCP, two TLS
  handshakes, one small JSON every `hb_s`. 160 MHz bought nothing measurable and cost
  ~20-30 mA continuously. **Also a live S0-fw-3 candidate**, which is why it is in
  `sdkconfig.defaults` next to `REDUCE_TX_POWER` and not in a performance note.
  `esp_clk_init()` applies it before `app_main`. Thus, it is in effect during PHY
  calibration. The CPU is running flat out alongside the calibration at 160 MHz. The
  v0.3.3 result ruled out TX power as the dominant draw. It did not rule this out.
  Untested on hardware as of this entry.
- **`WIFI_PS_MAX_MODEM`, stated rather than inherited**. IDF's default is
  `WIFI_PS_MIN_MODEM`. Thus, the radio already slept — but as an accident of the SDK's
  default that an IDF pin bump could change, in a file whose whole style is to say why.
  `MAX` rather than `MIN` because the workload already chooses to be deaf for tens of
  seconds (30 s MQTT keepalive, `hb_s` heartbeat). The cost is seconds of `dn/cmd`
  downlink latency, accepted. Every command this product sends is part of a deploy. No human waits on one interactively. Revisit if R2 grows an interactive command.
- **The Wi-Fi reconnect walks a TX-power ladder instead of repeating one attempt**.
  Retrying forever is only useful if the attempts differ. An identical attempt repeated
  for a year is a stuck board that looks busy. After every 3 consecutive failures the
  radio steps down (default → 14 dBm → 8 dBm) and wraps. The counter-intuitive part is
  that *lowering* power can make association succeed: the auth/assoc frames at full power
  are the biggest current transient in the sequence, and on a marginal rail that is what
  drops it under the brownout threshold mid-association.
  - **Runtime `esp_wifi_set_max_tx_power`, not compile-time
    `CONFIG_ESP_PHY_MAX_WIFI_TX_POWER`**. This is the per-board version of the knob
    S0-fw-3 deliberately refused to turn fleet-wide. It costs range only on a board that
    has already proven it cannot associate at full power, and only while that is true.
  - **Rung 0 comes from the driver, not hardcoded to 20 dBm**. On a post-brownout boot
    `CONFIG_ESP_PHY_REDUCE_TX_POWER` has already brought the PHY up at minimum power, and
    a ladder that "restored" a literal 20 dBm would silently undo S0-fw-3 on exactly the
    board we wrote it for.
  - **A working rung stays, not reset**. A board that could only associate at 8 dBm
    will not survive its first data frame at 20. The rail that failed during association
    was not fixed by it succeeding. Wrap-around still reaches full power again. This is what lets a board that was moved, or whose supply got a fix, climb back with no
    re-flash.

**The retry count is deliberately still unbounded** — considered and rejected in the same
review. A board that stops trying to reach its network is a site visit. This is the
intervention this product exists to delete, and an AP reboot or a day-long uplink outage
is survivable only by a board still trying when it ends. The fix for "stuck in a loop" is
to vary the attempt, not to stop making it.

Guarded by `tests/test_agent_power_and_size.py`, deliberately separate from
`test_agent_partitions.py`: everything here is OTA-recoverable. That file's value comes from every line in it being a physical-recall mistake. The size budget is per
target, ratcheted down after each measured win.

---

## 2026-09-13 — the flasher erases `nvs`, never the whole chip

Every flash used to pass `eraseAll: true` to esptool-js, which erases the entire chip —
including the `phy_init` partition holding the cached RF calibration. That was wrong, and
it is the one thing on our side that was demonstrably making the brownout loop
unescapable. The calibration is written only after a boot survives the full calibration,
the largest current draw in startup, so erasing it on every flash guarantees the expensive
path on every freshly flashed board, forever. A board with a marginal rail can never
bootstrap out. This is because the thing that would save it disappears on each attempt.

- **It also explains the comparison that kept confusing us**. Arduino's uploader writes
  bootloader, partition table and app and leaves `nvs` and `phy_init` alone. So "the same
  cable and port run a stock sketch fine" was never evidence that the supply is adequate —
  the sketch inherits a calibration it never has to re-earn. Ours re-earns it every time.
- **`nvs` still has to go**. A board that already enrolled keeps its broker credential
  there and reuses it (R0-fw-1 logs "reusing the stored credential"). Thus, the freshly minted
  token in `ff_cfg` would never be spent. Implemented as an explicit part in the write plan
 (0xFF over exactly that partition) because `writeFlash` erases the sectors it writes
  and NVS reads an erased sector as empty. esptool-js 0.6.1 has no `eraseRegion`, or this
  would be one call.
- **The parser reads the offset instead of hardcoding it**. `partitionTable.ts` parses the table being written
  to the board in the same operation and looks `nvs` up by label. `0x9000` is right for
  `ab-4m-v1` and need not be for the next layout, and a wipe aimed at a stale constant
  erases the wrong 24 KB of a real board.
- **`eraseAll` is gone from `WriteOptions` entirely**, not defaulted to false. Whether to
  clear credentials is a property of the write PLAN. Leaving the flag on the write CALL
  would put a whole-chip erase one boolean away from returning.
- **This does not fix the board that prompted it**. That board has never completed a
  calibration. Thus, there was nothing to preserve. What changes is that once any board gets
  through once (on a better supply, or with bulk capacitance) a reflash no longer throws
  it away. See the entry below for what is still unresolved.

---

## 2026-09-13 — reducing TX power does not break the brownout loop (supersedes 2026-09-12, S0-fw-3)

The entry below claims `CONFIG_ESP_PHY_REDUCE_TX_POWER=y` ends the loop. On the one board
that has ever been in the loop, it does not. Recording that here because the claim shipped
in v0.3.3 and an untested claim left standing is how the next person wastes an evening.

- **The evidence is an A/B inside one log**. Diagnostic bundle 2026-09-13T14:15, device
  `8c94df4cf3f8`, agent `19b0a0b`. Its first boot follows a non-brownout reset, so
  `esp_reset_reason() != ESP_RST_BROWNOUT` and the PHY started at full power. Boots two
  through six each print `the previous boot ended in a BROWNOUT`. Thus, the reduction was
  active. All six die identically at `phy_init: failed to load RF calibration data
  (0x1102), falling back to full calibration` → `E BOD: Brownout detector was triggered`.
  The 774 ms / 822 ms difference between them is the UART time to print that warning line,
  not progress.
- **The lever was aimed correctly. It just has no effect here**. IDF v5.5.5
  `components/esp_phy/src/phy_init.c` calls `esp_phy_reduce_tx_power(init_data)` before
  `register_chipv7_phy(init_data, cal_data, calibration_mode)`, and an empty NVS forces
  `calibration_mode = PHY_RF_CAL_FULL` regardless of `CONFIG_ESP_PHY_CALIBRATION_MODE`. Thus, the lowered power table is in force *during* the full calibration. The conclusion is not
  that the option was misapplied but that TX power is not what dominates a cold
  calibration's current draw on this hardware.
- **The change stays in**. It depends on the brownout reset reason, inert on a healthy
  board, and costs nothing. Reverting it would buy nothing either. What changes is the
  claim attached to it. It is a plausible mitigation with one negative result, not a fix.
- **The reporting half of S0-fw-3 stays intact and does work**.
  `agent_main.c::log_power_fault()` and the `brownout` progress stage behave exactly as
  designed — the warning line appears on every post-brownout boot in the bundle above.
  That half is what made this negative result legible at all.
- **What would actually settle it is a hardware experiment, not a firmware one**. Flash the
  stock Arduino Wi-Fi sketch with a full chip erase onto the same board, cable and port. A
  brownout there rules out every firmware avenue and points at bulk capacitance across
  3V3/GND. Survival there means our startup draws more than it needs to. Until that runs,
  "the supply is marginal" is the best-supported reading but is not proven against our own
  image.

---

## 2026-09-12 — brownout recovery is targeted, not a fleet-wide power cut (S0-fw-3)

A board with no cached RF calibration browns out inside `phy_init`'s full calibration and
cannot escape. The calibration is only written back once a boot survives it. Thus, every boot
is identical. Reported by an operator whose cable and port demonstrably flash and run a
plain Wi-Fi sketch — which they do because that sketch inherits a calibration it never has
to re-earn. The fix is **`CONFIG_ESP_PHY_REDUCE_TX_POWER=y`**: after a brownout reset the
PHY starts at its lowest TX power, often enough to get through once, and one survived
boot ends the loop for good.

- **Fleet-wide TX power stays at 20 dBm**. `CONFIG_ESP_PHY_MAX_WIFI_TX_POWER` was the
  obvious alternative and is the wrong trade: it costs range on every board in the fleet,
  permanently, to fix a fault some boards have on their first boot only. The IDF option
  above is the same idea aimed at the boards that need it. It depends on
  `esp_reset_reason() == ESP_RST_BROWNOUT` and is inert on a healthy board.
- **It lives in `agent/sdkconfig.defaults` but is NOT a flash-time immutable**. Everything
  else in that file is (partition table, bootloader rollback, the compiled-in CA bundle).
  This one ships in the app image and an OTA can add or delete it. It is there because
  that file is the one place the agent's posture comes from, and the comment says so.
  Deliberately **not** added to `REQUIRED_SDKCONFIG` in `tests/test_agent_partitions.py`:
  that list's criterion is "no OTA-free fix exists". An OTA-fixable entry would blur
  what the guard means.
- **The escape reports back, not just succeeds**. `agent_main.c::log_power_fault()` reads
  `esp_reset_reason()` and states the previous boot's brownout outright. The agent sends a `brownout` progress stage just after `link_up`. Both exist because the recovery
  is otherwise invisible: the board reboots, and the BOD line and the reset banner belong
  to the boot that died. A board that browns out and then recovers looked flawless on the
  dashboard and in the flashing console. `brownout` sits outside the stage walk (it is
  retrospective) which is why it reports after `link_up` and not before.
- **Why the banner is not reliable here**. `CONFIG_ESP_BROWNOUT_USE_INTR=y` means the
  BOD ISR restarts the chip. Thus, the next boot prints `rst:0x3 (SW_RESET)` rather than
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
uncommitted changes. A bundle records HEAD, not what the build compiled.

- **Why git ancestry, not mtime**. The TODO's literal wording was "bundle is older than
  `agent/main/`" by mtime. `git checkout`, `git pull` and branch switches rewrite source
  mtimes with no content change. A clone sets them all to clone time. A check that fires
  on a correct tree is the check people delete. The runbook already carries that lesson
  verbatim about a `CONFIG_SECURE_BOOT_V1_SUPPORTED` prefix match. Git ancestry answers
  the same question ("does this bundle contain the newest agent source change?") and
  cannot be wrong about it. This deviation from the TODO's wording is deliberate.
- **The dirty-tree refusal lives in the release path only**, never in `just agent-build`.
  Dirty-tree builds are the firmware dev loop (edit → build → QEMU → commit), and
  breaking that would get the guard deleted. The release path gets condition 4 because
  `make_manifest.py` records `git rev-parse HEAD`. Thus, a bundle built from a dirty tree
  claims provenance it does not have — the runbook already says "commit before building
  anything you intend to push", and refusing a dirty agent tree in `just build` closes
  the one place that rule is otherwise unenforced.
- **Source pathspec is `agent/` minus `agent/dist/`**, that is,exactly the Docker build
  context `agent/.dockerignore` defines (`COPY . /project`). Rejected narrower variants
  (per-target `sdkconfig.defaults.<target>`, "only `agent/main/`"): they drift from
  `.dockerignore`. The whole point is that anything that can change a bundle is
  counted. Over-strict costs a rebuild. Under-strict costs a fleet.
- **No bypass env var.** v0.3.0 shipped stale knowingly. The harm was that it became
  invisible afterwards. If a stale ship is wanted again, `just agent-build-all` is 20
  minutes, and deleting a justfile line is a reviewable commit.
- **The esp32 bundle was stale too**, which the filed task did not know. All four targets
  were rebuilt. `agent/dist/esp32/manifest.json` recorded `source_commit 81aea08`
  (S0-fe-7), while the newest commit touching agent sources was `43aeb31` (S0-fw-2,
  "hold pre-clock stage reports"). So the shipped esp32 bundle was missing the S0-fw-2
  fix — consistent with DECISIONS.md 2026-09-11 S0-fw-2: *"Real boards do not have this
  fix yet. No commit here can give it to them."*

Details: `docs/runbooks/agent-build.md` → *Staleness*, `agent/tools/check_bundles_fresh.py`
(the guard), `tests/test_agent_bundle_freshness.py` (8 cases).

## 2026-09-11 — a stage report that predates the clock is held, not lost (S0-fw-2)

`link_up` (the report the entry below calls "a board is visible as `arriving` before it
exists in the fleet at all") had never once reached a real server. It is sent the moment
the link starts. This is before `ff_time_sync()`, and `CONFIG_MBEDTLS_HAVE_TIME_DATE=y`
(R0-fw-1, deliberately) makes a TLS handshake at epoch 0 fail certificate validity. The
POST never opened, the failure logs at DEBUG by design, reports never retry. The
stage was gone. Reproduced against prod with the pre-fix bundle before anything was edited.

- **The gate is the URL scheme AND the clock. The scheme half is not defensive
  padding**. `transport_ready() := !tls || ff_time_is_sane()`. A clock-only gate is the
  obvious reading of the bug and it would have **regressed the one configuration where
  the feature already worked**. The plaintext lab runs `http://` with `--no-ntp`, the
  clock never becomes sane. `link_up` would were held forever. `tls` is decided
  once in `ff_progress_init()` from the scheme of the built URL. This is the same
  property `ff_cfg.h` already documents as selecting TLS. Both directions were run.
- **Buffer-and-drain, not "move the call below the sync"**. The two-line version makes
  `link_up` a lie about when the link started, delays the first sign of life by up to
  `SNTP_TIMEOUT_MS` (15 s), and fixes exactly one call site — the next pre-clock reporter
  re-introduces the bug. `agent_main.c` is comments-only for that reason.
- **Drain BEFORE the current report's POST**. The server timestamps at receipt and
  `progress.py` breaks `at` ties with `id`, so a held `link_up` inserted immediately
  before `time_synced` still sorts ahead of it. Drain-after would invert the pair in the
  same clock tick. Measured on prod: `link_up` 00:42:24, `time_synced` 00:42:27.
- **No `ff_progress_flush()`**. `agent_main.c` already reports `time_synced`
  unconditionally right after the sync, so the drain point exists for free and cannot be
  forgotten. A public flush would be an ordering trap for whoever omits it.
- **One attempt per held stage. The first failed send abandons the rest**. Property 2
  (never retried) applies to a held report as much as a live one, and abandoning bounds
  the added boot-path cost at one `PROGRESS_TIMEOUT_MS` (5 s) instead of depth × 5 s. A
  failed open means no route. The remaining stale stages are not worth 15 s of the enroll
  path.
- **Re-check `armed` after the drain**. A held entry can take the 401 branch, and
  continuing to POST the current stage afterwards is exactly the "keep talking with a
  credential the server called dead" behavior property 3 exists to prevent. This is the
  one ordering hazard the change introduces and it is invisible in a plaintext lab. A 401
  clears the queue as well. Those stages can never do anything but sit in RAM.
- **`FF_PROGRESS_MAX_STAGE` is 32, not 24**. The server's stage regex is
  `^[a-z][a-z0-9_]{0,31}$`, so 32 characters is the widest legal stage a future caller
  could pass. A silent `strlcpy` truncation would turn a valid stage into a
  *different* one. Retyped across the seam with the same comment `PROGRESS_MAX_DETAIL`
  carries. A held entry stores `stage` + `detail` only — never the token, which stays in
  `s_state` and is inserted at build time. Thus, the existing wipe-the-body discipline still
  covers every copy of it.
- **PROPOSED `spec/device-protocol.md` wording, deliberately not written** (same
  treatment S0-fw-1 gave `POST /v1/device-progress`). Under *Clock — SNTP before TLS*:
  "Stage reports (`POST /v1/device-progress`) produced before the clock is set are held by
  the agent and sent, in order, on the first report after the sync. Their `at` is the
  server's receipt time. Thus, a held stage reads as slightly late. The ordering is exact."
- **Real boards do not have this fix yet. No commit here can give it to them**.
  `agent/dist/` stays in .gitignore and the flasher serves the bundle baked in by `COPY
  agent/dist /app/agent` (R0-infra-2), so prod issues the buggy firmware until the next
  release build. Precisely the coupling *agent bundles are artifacts* (2026-09-11) exists
  to delete. Out of scope for a 0.25 d firmware fix.
- **Worth keeping: `-Werror` is the only static gate firmware gets**. There is no host-side
  C test harness in this repo. `agent-qemu-smoke` runs a **tokenless** config. Thus, the
  reporter is unarmed and the smoke check cannot exercise the queue at all. Green smoke
  means "still boots". The behavior is proved against prod or not at all.

Details: `docs/features/enrollment.md` → *The first stage survives the clock (S0-fw-2)*,
`docs/runbooks/agent-qemu.md` → *Proving the clock rule* (third direction).

## 2026-09-11 — the escalation path is one click, and redaction is not the firmware's job (S0-fe-7)

The fourth layer of *Unaided onboarding*. On 2026-09-11 the diagnosis was already on
screen and still had to be re-typed into a chat window, because selecting text in an
unlabelled `<pre>` is not an affordance anyone finds. **Copy diagnostic bundle** now
assembles the header, the fault, the boot progress, the chip, the config and the whole
console log into one string, puts it on the clipboard and renders it in a `readOnly`
`<textarea>`.

- **Redaction happens at one choke point, not per field**. `redactSecrets` applies
  once to the fully assembled string, as the last statement of `buildDiagnosticBundle`.
  A comment marks it as the only `return` that function can have. Per-field redaction
  fails open: the next section someone adds stays unredacted by default, and the section
  most likely to carry a live credential is the raw board log, which nobody remembers to
  filter. A bundle is *designed* to be pasted into a chat window. Thus, it is the single
  most likely way a live secret leaves the machine.
- **Three rules, because a secret arrives three ways**. (1) A literal scrub of what the
  page received (`split`/`join`, never `new RegExp(secret)` — a passphrase is arbitrary
  text and `.*` would eat the bundle), with a `MIN_SCRUB_LENGTH = 4` floor so a
  two-character "secret" cannot shred the log. (2) URI userinfo
  `scheme://user:pw@host` → `user:[REDACTED]@`, which reaches a broker credential this
  page was never given. This is because the *firmware* printed it. The username survives, it is
  diagnostic and not a credential. (3) Token shapes `ff[ae]_…` with a `{6,}` length
  floor, so the panel's own prose ("re-flash with a fresh `ffe_` token") stays readable.
  Each rule has a test that fails when we delete only that rule — checked by deleting
  each in turn.
- **The page prints two agent versions, not one**. What this page would flash (the manifest)
  and what the board says it is running (the `ff-agent` banner). They differ exactly when
  the board is carrying a stale flash. This is invisible from either number alone.
- **`window.location.origin`, never `href`**. A path or query string can carry a token.
  The origin is the only part that answers "which deployment is this?".
- **The `/v1/healthz` get for the server version is silent on failure**. It never sets
  `manifestError` and never triggers `onSessionExpired`. A bundle that cannot name the
  server version is still worth pasting. An unreachable server must not color the
  flasher page red.
- **The textarea shows a snapshot taken at the click, not a re-derivation**. The summary
  ticks at 1 Hz while watching. Thus, a derived box would drift from what the clipboard got
  within a second. It is set *before* the clipboard write. Thus, a browser that refuses the
  clipboard still leaves the full bundle on screen with a sentence saying so.
- **Deviation from the plan**. The plan's format example echoed the offending log line in
  the fault section. That made `E BOD: Brownout detector was triggered` appear four times
  while the same plan's acceptance requires exactly three (once per cycle, so "how many
  times did this board brown out?" is answerable by eye). The fault section thus
  prints the plain-English hint plus `named by  line N of the console log below` and
  never reprints the line. The diagnosis still lands in the first fifteen lines.

## 2026-09-11 — agent bundles are artifacts, not image contents (planned, infrastructure)

The application image will ship **zero** agent firmware bundles. They move behind
`ObjectStore` onto the same distribution path as R1's user artifacts, so publishing a
bundle stops requiring an app rebuild and deploy. Reverses the distribution half of
R0-infra-2's `COPY agent/dist /app/agent` (its build half stays the same) on two grounds:
the target list only grows (~1.2 MB per chip, with H2 / Thread / RPi already on the
roadmap), and the coupling produced its defect once — `S0-infra-2`, three
stale bundles shipped in v0.3.0. A baked fallback tier was not our choice as
preserving the defect with an extra branch. Accepted cost, stated: onboarding comes to
depend on a store that today cannot be credentialled, making the GCS blocker in
`docs/runbooks/artifact-storage.md` a hard prerequisite rather than a caveat. Details:
[design/decisions/infrastructure-agent-bundles-are-artifacts.md](design/decisions/infrastructure-agent-bundles-are-artifacts.md),
[docs/features/infrastructure.md](docs/features/infrastructure.md) → *Agent bundles served from the object store*.

## 2026-09-11 — the panel does the fix it names (S0-fe-6)

Layer 3 of *Unaided onboarding*. The panel already named the fault (S0-fe-4) and always saw
the boot (S0-fe-5). It still answered a spent token with three manual steps written as prose.

- **There are exactly two mechanisms, because the panel's only channel to the board is the
  serial port:** `reboot` (pulse EN through the console session) and `reflash` (release the
  port, re-acquire with esptool, mint, write, erase). "Retry enroll" and "mint a fresh token"
  from the task text are **not** separate actions and collapse into `reflash` — the agent
  exposes no serial command surface. A token that is not written into `ff_cfg` changes
  nothing. Anyone who later wants a third remedy needs a new channel first.
- **The rule that decides who gets a button. The board will not fix itself AND the action
  changes the outcome**. `agent_main.c` is what fills the table in, not taste.
  `enroll_until_credentialed()` parks forever on 401/409, so only a re-flash moves that
  board. Everything else retries by itself (60 s → 15 min for a 503, `while
  (ff_net_bring_up(...) != ESP_OK)` forever for the link, DHCP forever) and **the agent's
  own retry ladders are precisely what disqualify 503, DHCP and link from having a button**.
  A button that restarts a retry already in progress is the thing the task forbids.
  Brownout and wrong PSK render no button because no software fixes a cable or a PSK.
- **The recovery re-flash always erases. It is deliberately NOT the form's checkbox**.
  Every fault a re-flash fixes is a spent token or a stale NVS credential, and
  `ff_store_load()` short-circuits enrollment while a credential is present — so a freshly
  minted token written beside it is dead on arrival and the operator sees the *same* fault
  after pressing the button. That is the worst possible outcome for a feature whose entire
  point is that the button works.
- **The log is deliberately not cleared on recovery**. The re-flashed board's first
  `ff-agent` line is a `boot` milestone, which clears the fault by S0-fe-4's existing rule —
  so clearing the log would be redundant on success and destructive on failure, where the
  evidence is the only thing S0-fe-7 will have to bundle.
- **`halted:` is a GENERIC hint, which corrects the remedy table the plan shipped with**.
  `park()` logs its reason strictly *after* the failure it reports. Thus, a specific
  classification replaced "this token is single-use, flash the board again to mint a fresh
  one" with the engineer-facing "re-flash ff_cfg with a fresh ffe_ token (POST
  /v1/enrollment-tokens)" on the flagship case. Same derivative shape as `Backtrace:` and
  `SW_CPU_RESET` — the third time that pattern was the right answer. It is, but,
  the one generic hint that **does** carry a remedy: the ban on the others exists because
  their fix belongs to the specific line above them, whereas re-flash is the same action
  whatever evidence names it, and a board that parks with nothing diagnosed above it (`no
  usable ff_cfg partition`, `no eFuse MAC`) has no other line to carry the button.
- **Gotcha that would cost an afternoon: Testing Library matches accessible names by
  substring**. `Re-flash this board` contains `flash this board` and turns every existing
  `getByRole('button', { name: /flash this board/i })` into "found multiple elements". A
  second `Reboot the board` breaks the EN-pulse test the same way. The labels are thus
  `Re-flash the board` and `Reboot and retry`, pinned in `REMEDY_LABELS` so the panel and
  the tests cannot drift.
- **`flash()` stopped reading the `chip` state and reads `chipRef` instead**. `reflash`
  calls `connect()` and `flash()` in the same tick. The state has not re-rendered. The
  engine would selected the bundle for whichever board was on the desk last time. An S3 bundle flashed to a C3 erases cleanly and never boots. `setChip` stays. The view
  renders from it. This is the one change in the task with a real cost when wrong.
- **At most one action on screen**, fault first and the overdue banner only when the fault
  has none. Two identical buttons are confusing to the operator and an ambiguous
  `getByRole` for every future test.
- **A missing user gesture now says something true**. `requestPort()` needs transient user
  activation and the recovery click awaits `release()` first. Chromium's `SecurityError`
  for a closed activation window used to map to "the page must be on HTTPS or localhost".
  This is wrong and sends the operator nowhere. Do not "fix" the underlying race by
  calling `onReflash()` without awaiting the release. The flasher would then open a port
  the console still holds and get `InvalidStateError` most of the time.
- **The loop closes itself and no code makes it happen**. The recovery flash drives `phase`
  through `flashing` → `done`, so `autoWatch` toggles, the panel's `armed` ref resets, and
  `watch('granted')` re-opens the port and pulses EN (S0-fe-5). Nothing went in for it.
  Do not add anything.

Details: `docs/features/enrollment.md` → *Recovery is a button (S0-fe-6)*.

## 2026-09-11 — the console resets the board itself so the boot is never missed (S0-fe-5)

The panel's automatic reset is not a fix for a dropped session. It is the *designed
behavior*. The flasher and the console are separate sessions at different bauds, and the
window between `hard_reset` and the console opening at 115200 is long enough (up to 8 s on
native-USB parts) to lose the entire boot. So every `watch()` pulses EN once, turning "the
board was silent when we arrived" into "the board prints its first line while we are
listening".

- **The pulse happens on every path that opens the port, automatic or manual**. A board
  watched five minutes after flash has the same silence. A purely-automatic pulse would leave
  the manual case (click **Watch a board** without flashing first) broken. Both paths pulse.
- **`commandedReset` travels in-band as an event property, not as a second argument**. The
  reboot-loop suppression depends on knowing which boot the panel asked for.
  `summarizeConsole` is pure over `events` alone (every test builds summaries from arrays,
  the hook memoises on `[events, now]`). Thus, the flag rides in the event.
- **Order is load-bearing in `watch()`: append notice synchronously, attach the reader
  immediately, let the 150 ms EN pulse run concurrently**. Awaiting `session.reboot()` first
  would leave the port unread for 150 ms—Chromium's default 255-byte read buffer is ~22 ms
  at 115200 baud. Thus, the panel can overrun. Appending the notice *after* the pulse makes its
  position in the stream racy, breaking the one-boot-deep suppression.
- **A failed pulse degrades to a notice, never to an error**. `setSignals` can be
  unsupported or wired differently. The operator sees a log region with "could not reset the
  board. Press 'Reboot the board'", never a red fault panel.
- **Native-USB re-enumeration is acknowledged but not solved here**. C3/C6/S3 drop off the
  bus on reset and return as a new `SerialPort`. The disconnect message now says "dropped off
  the USB bus when reset — some boards re-enumerate. Press 'Watch a board' to pick it up
  again." Automatic re-acquire is S0-test-2, blocked on hardware.
- **Deliberate residual: a board mid-OTA-download gets restarted**. The panel has no way to
  know an OTA is in progress (the agent prints no such line), and waiting to determine would
  recreate the silence problem. Acceptable at R0 (the console is a bench tool, and an
  interrupted OTA drops rather than committed). Documented so it is not later reported
  as a mystery.
- **Gotcha for the next console change: the summary is now a function of the clock**.
  `useBoardConsole` ticks once a second while watching (not idle). In tests, `vi.useFakeTimers()`
  + `vi.advanceTimersByTimeAsync()` inside `act()` drives both the tick and `Date.now`, which
  is the only way to test a deadline with no new line arriving.

Details: `docs/features/enrollment.md` → *The boot appears automatically (S0-fe-5)*.

## 2026-09-11 — a milestone is a claim about now. A fault is a record of what happened (S0-fe-4)

Implements the first layer of the standard set by the entry below. Three things were
decided while fixing it that are not obvious from the task text.

- **The parser must match the whole line, not the message**. `hintFor` was keyed on the
  ESP-IDF tag. That is why `E BOD:` was invisible. The fix could were "add a rule for
  tagless lines". Instead every bare rule matches against the **cleaned whole line**. Thus,
  the same rule fires whether ESP-IDF prints `E BOD: …` early-boot style or `E (403) BOD: …`
  through the normal logger. A classifier keyed on a *format* is what broke. Keying the
  content match on the format again would rebuild the same trap one layer down.
- **`reached` clears on a reboot and `fault` is not. That asymmetry is the point**.
  A milestone is a positive claim that must be true *now* — a stale ✓ actively misdirects.
  This is precisely what cost the bench session. A fault is a description of something that
  happened. The reset it caused does not make it untrue. A panic prints its cause
  immediately *before* the reset that would otherwise erase it. So progress clears a fault
  (unchanged) but a boot boundary does not.
- **"Twice is proof of a loop" needed a second clause, or the happy path cries wolf**. The
  filed task said seeing `boot` twice proves a reset loop. Literally true on a stranded
  board, false the moment the operator presses **Reboot the board** on a healthy one — and
  S0-fe-5 is about to make the panel reboot boards by itself. `rebootLoop` is thus
  raised only when a boot starts while the *previous* boot had not reached the fleet, and clears on one that does. Same detection on the failure case, silent on the success case.
- **A generic hint is the right answer more often than it looks**. `Backtrace:` and
  `SW_CPU_RESET` both *look* like specific diagnoses and are both consequences printed after
  the line that actually names the cause. Classifying them `kind: 'generic'` reuses the
  existing "never overwrite a named cause" rule instead of adding ordering logic. Worth
  reaching for whenever a line is real but derivative.
- **Deadlines are grounded in the agent's own constants, not chosen**. `NET_TIMEOUT_MS` 30 s,
  `SNTP_TIMEOUT_MS` 15 s, `ENROLL_TIMEOUT_MS` 30 s, each plus room for one retry, and each
  measured from the *previous* milestone. A deadline shorter than the board's own patience
  would report a fault the board has not had yet. The numbers are not in `spec/prd.md`'s
  targets table. Proposed for it rather than written there.
- **Gotcha for whoever tests the next layer:** the summary is now a function of the clock, so
  `useBoardConsole` ticks once a second **while watching only**. An idle panel must not
  re-render forever. In jsdom, `vi.advanceTimersByTimeAsync` inside `act()` drives both the
  tick and `Date.now`, which is the only way to test a stall that arrives with no new line.
- **The bench log is a reconstruction, and the fixture says so in its header**. The real
  capture was pasted into a chat and never committed. That is not a documentation lapse to
  be tidied up. It is the exact failure S0-fe-7 exists to delete. Thus, it goes into the record rather
  than glossed.

Details: `docs/features/enrollment.md` → *The console always names a diagnosis (S0-fe-4)*.

## 2026-09-11 — the console panel is the diagnostic surface of record for onboarding

The first hardware bench found no server bug and three onboarding bugs. A DevKit v1
brownouts during Wi-Fi PHY calibration and resets forever. It printed `E BOD: Brownout
detector was triggered` on every cycle. The panel showed a stale green **Network
up** instead. The fault was found by pasting a UART log into a chat window.

Nothing server-side could helped — a board that never associates is invisible to
`device_progress` by construction, which `progress.py` already says out loud. The
board's UART is the only witness and the browser is the only listener. So the panel,
not the server, is held responsible for explaining everything between "flashed" and "on
the fleet", against a technician who does not know what a brownout is. Anything the
board says that the panel cannot explain is now a defect.

Gates R0, because R0's stated risk *is* onboarding. Full reasoning and the rejected
alternative (structured faults from firmware) in
`design/decisions/enrollment-console-is-the-diagnostic-surface.md`. Requirements in
`spec/standards.md`. The parser-versus-firmware trade and the missing CUJs in
`spec/open-questions.md`.

## 2026-09-11 — `arrivals` needed a second clause, not a tweak (S0-fe-3)

Closes the item filed at the end of the S0-fw-1 entry below. The question was whether "not
currently online" must also exclude boards that have *been* in the fleet. The answer is yes — but the interesting part is what the second clause had to consist of.

- **"Not online" was never the right question. "Has this board already arrived?" is**.
  A board on its way up and a board that started an hour ago and lost power are both
  offline with a recent stage. One clause cannot separate them. The symptom was a
  completed arrival re-entering the list labeled *stalled at `mqtt_connected`* for the
  rest of the 900 s window, duplicating an offline row directly above it. The new predicate
  `progress.has_already_arrived` asks the second question, and it lives in `progress.py`
  next to `stalled` rather than inline in the router. Thus, there is one place that decides it.
- **`broker_provisioned_at`, not `enrolled_at`**. The task's suggested shape named both.
  `enrolled_at` is `NOT NULL` with a default, so testing it decides nothing. A conjunct
  that is always true reads like a safeguard and is not one. The provisioning timestamp is
  the real end of the arrival sequence.
- **The re-flash case works for free because `last_seen` is monotonic and re-enrollment does
  not touch it**. Two decisions made elsewhere and for other reasons (`registry.py` leaving
  `last_seen` alone on re-enroll, `ingestor/store.py` advancing it with `GREATEST`) mean a
  re-flashed board's fresh stages are *necessarily* newer than its stale `last_seen`. So
  `stage_at <= last_seen` distinguishes "this stage belongs to the arrival that already
  finished" from "this board is arriving again" without a re-flash flag, a generation
  counter or a new column. **Worth noticing as a pattern: when a new rule needs to tell two
  situations apart, check whether an existing monotonic timestamp already does it.**
- **A deliberate residual. Thus, nobody reports it as a regression**. A board that reports
  `mqtt_connected` and dies before any live message advances `last_seen` past that report
  still reads as arriving. It never completed a heartbeat. Fixing it would require deciding
  how many messages count as "arrived". This is a worse rule than the honest edge.
- **The live vacuity check found nothing but is why the evidence is trustworthy**. Neutering
  the clause in the running api reproduced the original duplicate row verbatim against the
  same database state that had just shown `arrivals: []`. That is a stronger statement than
  a passing test. This is because it proves the empty list came from the rule rather than from the
  progress window having quietly expired.

Details: `docs/features/enrollment.md` → *An arrival that finished stops arriving (S0-fe-3)*.

## 2026-09-11 — the stage reporter has now run on a board, and one acceptance was wrong (S0-fw-1)

Supersedes the 2026-09-10 S0-fw-1 entry below, which recorded the server half and said
`ff_progress.c` had exactly one guarantee: that it compiles. It has now been executed.
The design decisions in that entry all stand. These are what running it added.

- **Acceptance 1 asked for something the feature does not claim. That is a spec bug,
  not a test bug**. "A board flashed with a deliberately wrong PSK shows a stalled stage
  rather than nothing at all". But a wrong PSK means no link, and `ff_progress.h`'s
  header already states that a board with no route reports nothing. The criterion and the
  interface contradicted each other, and the interface is right. Owner-confirmed
  substitution: the two cases the feature *does* claim, a board stalled at `enrolling`
  (broker down, so `/v1/enroll` 503s on provisioning) and one stalled at `mqtt_refused`
  (credential rotated out from under it). Both now pass, driven by `ff_progress.c` rather
  than by curl. **When an acceptance and an interface disagree, check which one was
  written after the thing was understood.**
- **The invisible case is real, measured, and belongs in the operator's head**. A board
  whose token triggers a refusal reports *nothing*: the progress 401 self-disables the reporter.
  Thus, the `halted` that `park()` tries to send never leaves the board. Checked with a
  revoked token — zero rows, silent dashboard, and only the `ff-progress` warning on the
  console. This is the designed behavior and it is also the feature's ceiling: [[S0-fe-1]]
  (serial) and this task cover disjoint failures, and neither is a substitute for the other.
- **`progress_stall_s` (60 s) and `ENROLL_RETRY_MIN_MS` (60 s) are the same number. Thus, a
  freshly-stalled board flickers**. The row alternates `stalled=false/true` for the first
  couple of minutes and only settles once the backoff doubled past the threshold.
  Observed, not theorised. Left alone rather than tuned. The flicker is honest (the board
  *is* alternating between reporting and waiting) and changing either constant to fix a
  cosmetic wobble would trade a real property for a UI one. Worth knowing before someone
  reports it as a bug.
- **A trap that cost a full round of acceptance evidence:
  `docker ps --filter ancestor=espressif/idf:v5.5.5` matches nothing when the image is
  digest-pinned**. The filter compares the reference you typed, not the image the container
  runs. Thus, it exits 0 having killed nothing — indistinguishable from "no emulators are
  running". Killing the `just` process does not help either: without `-it`, `docker run`
  leaves the container alive. Six emulators accumulated, all claiming `000000000000`, all
  enrolling and heartbeating over each other, and the first set of results had to be
  discarded. Fixed at the source rather than in a doc: `agent-qemu` names its container
  `ff-qemu-<target>` and **refuses to start a second one**. `agent-qemu-stop` exists.
  Vacuity-checked both ways. **Apply the general rule: if a cleanup command can fail
  silently, the thing it cleans up needs a name.**
- **A board is visible as `arriving` before it exists in the fleet at all** — the
  `link_up` arrival lands before any `devices` row does. That is the whole point of the
  feature and it is worth stating as an observed fact rather than an intent.
- **Filed, not fixed: an offline board reappears in `arrivals`**. Once presence decays,
  a board that got all the way to `mqtt_connected` and then died appears as "arriving,
  stalled at `mqtt_connected`" for the rest of the 900 s window, duplicating a row the
  fleet list already shows as offline. The `arrivals` rule (recent stage + not online) is
  doing exactly what it says. Whether "not online" must also exclude boards that have
  *been* in the fleet is a dashboard decision. Thus, it is **S0-fe-3** rather than a quiet
  change here.

Details: `docs/features/enrollment.md` → *Boot & enroll stage reports (S0-fw-1)*,
`docs/runbooks/agent-qemu.md` → *Reproducing a board that gets partway*.

---

## 2026-09-11 — the QEMU harness was never broken. It was unrunnable and unverifiable (S0-infra-1)

- **The filed root cause was wrong and the named suspect is innocent**. S0-infra-1
  reported a `LoadProhibited` boot loop inside `esp_task_wdt_init` and suspected the
  `-global driver=timer.esp32.timg,property=wdt_disable,value=true` flag. It did not
  reproduce: a full run from a wiped flash image and a fresh token reached `enroll 200`
  → `mqtt connected` → heartbeats, and five further boots (three under eight busy-loops
  on a four-core box) produced zero panics. Every one of those runs carries the flag.
  **Recorded so nobody re-decodes that backtrace**: `docs/runbooks/agent-qemu.md` →
  *What we know about the boot-loop panic*.
- **`docker run -it` in a recipe is a bug, not a convenience**. `agent-qemu` passed it
  unconditionally. Thus, the recipe died with "cannot attach stdin to a TTY-enabled
  container" in every agent session, script and CI shell. That is,it could not run by
  the things that most need to run it, including the acceptance criterion of the task
  filed against it. Whoever hits that hand-rolls a `docker run`, and a hand-rolled
  emulator invocation is where a wrong `-M`/`-m`/`-global` and an inexplicable watchdog
  panic come from. This is the most probable origin of the reported backtrace. `-it` is
  now conditional on `[ -t 0 ]`. **Apply this to any recipe that shells into a
  container.**
- **A digest pin does not pin what you run. Docker checks a digest on `pull`, not on
  `run`**. A damaged or replaced local layer runs in silence. `qemu-system-xtensa` lives inside the pinned image. This was in fact *absent* from
  this box's store when the investigation started and had to be re-pulled, with `/` at
  85%. Since every other input to a boot is content-addressed (bundle sha256s, IDF's own
  `default_efuse` bytes, `esptool merge_bin` over manifest offsets), identical declared
  inputs produced different behavior. Thus, one input was not what it claimed. New
  `qemu_sha256` hashes the emulator **binary** inside the container before every boot and
  prints its version into every transcript. Limit stated rather than papered over: one
  binary is not the whole image.
- **The two QEMU recipes splice one `qemu_program` definition**. A smoke check that
  assembles its own machine guards a lookalike, not the recipe. Everything that varies
  arrives as an environment variable so the argv cannot drift.
- **`just agent-qemu-smoke` — the cheap answer to "is the harness alive?"** ~17 s, no
  token, no stack, no board. Asserts only the first seconds, most-specific first: no
  panic, exactly one ROM `rst:0x` banner, the `ff-agent` banner, `ff_cfg v1 loaded`. It
  deliberately proves nothing about enrollment or MQTT. **This task cost a decoded
  backtrace and a blocked firmware task to answer a question worth seventeen seconds**.
  Vacuity-checked both ways: a wrong `qemu_sha256` trips the integrity guard, and 256
  scribbled bytes in `app.bin` produce a real loop — `the board reset 27 times`.
- **Consequence for [[S0-fw-1]]: it has no blockers. Its firmware has already run**. The
  bundle in `agent/dist/esp32` is the S0-fw-1 build (`ff_progress` in `app.bin`,
  `source_commit f81d6f1` + dirty tree) and it boots and enrolls. The two firmware
  acceptances it could not reach are now executable on this box.

Details: `docs/features/infrastructure.md` → *The QEMU harness, re-checked*,
`docs/runbooks/agent-qemu.md`.

---

## 2026-09-10 — the theme is monochrome, so every state encodes twice (S0-fe-2)

- **One hue ramp means color is no longer available as a carrier of meaning. That is
  a functional consequence rather than a stylistic one**. On this palette green and red
  are the same grey. So `.ok` and `.bad` both go bright + bold and `.bad` additionally
  underlines. `.warn` goes body-weight + bold. `.muted` stays dim + normal. Every state
  in the app now differs from its alternative on at least two of {glyph, weight,
  lightness}. WCAG 1.4.1.
- **The audit came before the restyle. It is why this touched one component**. Every
  `.ok`/`.bad`/`.warn`/`.muted` site passed checks for whether color was its *only*
  carrier. Nearly all already carried their own text — "active"/"used", "Live", full
  sentences, the checklist's `✓`/`…`/`·`, and log lines that start with ESP-IDF's own
  `E (…)`/`W (…)`/`I (…)`. Exactly one was color-alone: `FleetView`'s `StatusCell`,
  which drew the same `●` for online and offline. It is now `█` versus `░`. If a future
  change introduces a second such site, the audit (not the palette) is what has to be
  re-run.
- **`.log .bad` cancels the underline that `.bad` carries everywhere else**. Inside a log
  panel the level is already in the text. Thus, the underline adds no information and
  destroys the monospace grid. A rule that exists to be *absent* in one place is worth
  the two lines of comment it has.
- **Two font stacks, and the split is load-bearing**. Press Start 2P applies only to short
  chrome (`h1`/`h2`/`h3`/`th`/`legend`/`button`). Device ids, table data, inputs and both
  log panels stay on a real monospace at full size. The task's constraint was that a
  pixel font must not cost the legibility of the two things operators actually read. A scoped show face is how that is *met* rather than hoped for. Do not unify them: a
  12-hex device id in Press Start 2P is ~2.5x wider and the face has no bold. Thus, the log
  panel would lose its weight ramp too.
- **The font is self-hosted, latin subset only**. `@fontsource/press-start-2p`, 12 KB
  woff2, bundled by Vite and checked present in the production nginx image. A Google
  Fonts `<link>` would were fewer characters and would added a third-party
  get, a CSP consideration and a hard dependency on the box having a route out — for a
  single-tenant appliance whose V2 promise is self-hosting, that is the wrong trade.
- **The greyscale check runs as a live CSS filter on the page, not as a post-hoc PNG
  conversion**. Converting the screenshot only proves the screenshot is grey. Filtering
  at render time also catches anything that would reintroduced hue — an accent, an
  emoji, a form control drawn by the UA. It passes trivially today precisely because the
  palette is achromatic. That triviality is the point.
- **Gotcha, cost twenty minutes: a Docker named volume seeds from the image exactly
  once**. After adding the font to `frontend/package.json`, `just rebuild frontend` built
  a correct image that the stale `ff_node_modules` volume then masked, and Vite reported
  `Failed to resolve import` for a package plainly installed on the host. The volume must
  be *deleted*. The override file's comment said "rebuild after changing package.json",
  which is true and insufficient. It now carries the four-line recipe.
- **The frontend test suite cannot regress on a pure restyle. It is worth knowing
  why**. Only `main.tsx` imports `index.css` and vitest never renders styles, so CSS is
  invisible to the 108 tests. The `StatusCell` glyph was the one change with any reach,
  and nothing asserts on it — the tests assert roles and labels. A restyle that *does*
  break a test changed the markup more than it meant to.

Details: `docs/features/dashboard.md` → S0-fe-2 (new capability area, registered in
`docs/roadmap.md`). T2 harness: `frontend/scripts/theme-shots.mjs`.

---

## 2026-09-10 — a boot stage reports over HTTPS under the enrollment token (S0-fw-1, attempted)

**Status: the server half shipped and undergoes a check. The firmware half is written, compiles
and has never run**. S0-fw-1 stays `- [!]` in TODO.md for that reason. Read the last bullet
before trusting `agent/main/ff_progress.c`.

- **PROPOSED protocol addition, deliberately NOT written into `spec/`**. A board between
  "flashed" and "online" holds no MQTT credential (that is the thing it is trying to
  get) so a stage report cannot travel on MQTT. The only credential it has is the
  `ffe_` enrollment token from initial flash. The only channel is the HTTPS one
  `/v1/enroll` already uses. Hence `POST /v1/device-progress`, token in the body, `202`.
  `spec/device-protocol.md` is the frozen v1 wire contract and stays frozen until this passes. The endpoint's module docstring says so at the top so the two cannot silently
  diverge in the reader's head.
- **The token undergoes a check and never burned. That distinction is the whole security
  argument**. `auth.enrollment.BURN_SQL` is not imported by the progress router and must
  never be: a board reports `enrolling` several times before it succeeds, and a reporting
  path that spent tokens would turn a debugging aid into a way to strand boards.
- **An already-burned token is still accepted, but only from `used_by_device_id` and only
  before `expires_at`**. Without that exception the two most valuable stages — `enrolled`
  and `mqtt_connected`, which by definition happen *after* the burn — could never be
  reported at all. It widens nothing: the predicate names one device, and the endpoint
  issues no credential, provisions nothing and writes no `devices` row.
- **`stalled` computes on read and never stores** (`progress_stall_s`, 60 s), in one
  function, exactly as `online` comes by `presence.is_online`. A stored `stalled`
  would need a sweeper and would be wrong between sweeps.
- **`arrivals` rides on `GET /v1/devices` instead of getting an endpoint**. The dashboard
  already re-reads that on every `ff_events` hint, so arriving boards cost no second get
  and no poll. Arrivals are boards with a recent stage that are **not currently online**,
  decided by the same `is_online` call that fills the rows above them. Thus, a board leaves
  the arriving list at the exact moment it really joins the fleet.
- **The table is bounded on write, not by a sweeper**: 20 rows per device, trimmed in the
  same transaction as the insert. A board retrying enrollment every 60 s reports forever,
  and a debugging table must not be able to outgrow the fleet it describes. Known bound,
  accepted: an unspent valid token can name any `device_id`. Thus, it can seed rows for
  arbitrary ids at the rate limiter's ceiling — small rows, 24 h token TTL, single-tenant.
- **No PG enum and no CHECK on `stage`**. The R0 agent is flash-baked. The server must
  tolerate an agent it can never update, including one that invents a stage. The API
  bounds the string's *shape* (`^[a-z][a-z0-9_]{0,31}$`, no control characters in
  `detail`), never its vocabulary. `ProgressStage` in `db/models.py` is advisory, and
  `FF_PROGRESS_*` in `ff_progress.h` are plain strings for the same reason. Checked: an
  unknown stage (`teleported`) persists and rendered as itself.
- **The SSE frame carries no `detail`**. `detail` is device-controlled free text. The
  event is a hint and the client re-reads, as for every other event type.
- **The honest limit is designed for, not around: a board with no route to the server
  reports nothing**. Stated in `ff_progress.h`'s header so nobody builds on a promise it
  cannot keep. This is for boards that get *partway*, and never a substitute for the
  serial console ([[S0-fe-1]]).
- **The firmware could not run. The harness is why**. `just agent-qemu esp32`
  boot-loops on a `LoadProhibited` panic inside `esp_task_wdt_init` before `app_main` —
  **reproduced at unmodified HEAD**. Thus, it is not this change. The Mac is the flashing
  bench. Thus, there is currently no way to execute agent firmware on this box at all. Filed
  as **S0-infra-1** with the decoded backtrace. until we fix it, `ff_progress.c` has
  exactly one guarantee: it compiles under `-Wall -Wextra -Werror`.

Details: TODO.md → S0-fw-1 (`- [!]`) and S0-infra-1.

---

## 2026-09-10 — the board's console belongs in the browser, as a second session (S0-fe-1)

- **`flash.ts`'s Rule 4 stays. The port is always released in a `finally`**. The task was
  filed as "keep the serial port after flashing". That framing is wrong. esptool-js's
  `Transport` owns the port at the *flash* baud, which is not the console baud. Thus, a held
  flasher would have to be reconfigured anyway — and unwinding the `finally` would leak a
  port on every error path in a file whose whole discipline is that it never does. The
  console is instead a **separate session against the same physical port**, opened after
  the flasher lets go.
- **It works with no user gesture because nothing ever calls `port.forget()`**. That rule
  went into `esptoolFlasher.ts` for a different reason (not making the operator
  re-pick the chooser for each board). It is what now lets an effect call
  `navigator.serial.getPorts()` after a flash and get the port back. Two features rest on
  that one line. Do not "tidy" it away.
- **The classifier ranks hints by specificity, not recency**. First cut showed the most
  recent explained line. On a stranded board that is always `agent_main.c:167`'s "no
  network yet. Waiting for the link", reprinted every 5 s, which buried the `reason 201`
  above it. Hints carry `hintKind: 'generic' | 'specific'`. Generic fills an empty slot and
  never displaces a named cause. Caught by a test written from the real 2026-09-10 log —
  the value of using a genuine failure as a fixture rather than an invented one.
- **A milestone clears the fault**. Wi-Fi retries are normal on a busy AP. Leaving "the PSK
  is wrong" on screen after the link started would be a lie the operator would act on.
- **No inactivity timeout, and this is load-bearing**. `agent_main.c:166` retries forever
  at 5 s. Thus, the failure mode has no window. Any timeout would drop precisely the slow
  failure the panel exists to find. Documented in the interface, not just the code.
- **Hardware properties are a separate task, not a hand-wave**. Four things (port
  re-acquisition after `hard_reset` on native-USB parts, 115200 decoding, the EN pulse
  landing in the app rather than the ROM loader, `screen` getting the device after Release)
  are properties of a bridge chip and an OS and cannot be proven in jsdom. Filed as
  **S0-test-1** with the specific failure signature to look for in each, rather than left
  as an implied "must work". The bench is the Mac.
- **Every log string the classifier matches is quoted in `boardConsole.test.ts`**. It is a
  contract with `agent/main/*.c` that nothing else enforces: reword an `ESP_LOGW` and the
  panel would silently stop diagnosing. The tests fail instead.

Details: [docs/features/enrollment.md](docs/features/enrollment.md) →
*Serial console after flashing (S0-fe-1)*.

## 2026-09-10 — fleetforge goes live on prod: an alias network, an infra ingestor, a TLS simulator (R0-infra-5)

- **A dedicated `fleetforge` network exists solely to carry the alias `api`**. The
  frontend's nginx has `proxy_pass http://api:8000` compiled in. The production box
  already runs a `content-api`. The alternatives were rebuilding the image with a renamed
  upstream (couples every future frontend build to one deployment's naming) or templating
  the nginx config at start-up (a whole mechanism for one string). A third network that
  only these three containers join is cheaper than both, keeps `backend` clean. The
  next fleetforge image works unmodified.
- **The ingestor is `INFRA_SERVICES`, never `APP_SERVICES`**. `docker rollout` runs two
  copies during the swap. The ingestor is the fleet's sole MQTT subscriber. Thus, that
  duplicates every telemetry row. Same reasoning that already keeps `mosquitto` out. This
  is a correctness constraint on the deploy script, not a preference. It is commented at
  both sites in `deploy.sh` and in the compose file.
- **The api migrates. The ingestor must not**. `RUN_MIGRATIONS=true` on one container
  only. Two processes racing `alembic upgrade head` deadlock on a slow migration.
- **Gotcha — `01-init.sh` is disaster recovery, not deployment**. `docker-entrypoint-initdb.d`
  runs only on an empty data directory. Adding the `fleetforge` role there created nothing
  on the running box. It had to consist by hand with `psql`. The file must stay in sync
  with what we created manually, and now says so.
- **Gotcha — a missing smoke-test case rolls back a healthy deploy**. `--service fleetforge`
  had no entry in `smoke_endpoint_for_service`. Thus, the check curled an empty URL, got HTTP
  000 and fired the auto-rollback while every container was in fact healthy. Adding a
  service to the filter without adding its endpoint is a trap. Commented at the function.
- **Gotcha — argon2id in `.env` must be single-quoted**, or compose eats the `$argon2id`/
  `$v`/`$m` segments and login can never succeed. The R0-infra-3 hash had exactly this
  problem and its plaintext was unrecoverable. Thus, the admin password was reminted here.
  Check with `docker compose config | grep -i ADMIN_PASSWORD_HASH`. A literal `$$` there
  is correct.
- **The simulator learned TLS (`--tls`), verification only, no pinning**. Acceptance needed
  a board over `mqtts://…:8883` and R0-test-1 left TLS as an explicit TODO. Off by
  default because the dev broker is plaintext behind Traefik. Worth knowing: without the
  flag against a TLS listener the connect does not error, it *hangs* — which reads like a
  firewall problem and sent the first attempt down the wrong path.
- **No object store, on purpose**. `constraints/iam.disableServiceAccountKeyCreation`
  blocks minting the GCS key and no R0 route touches the store. R1 waits on it.
  Tracked in docs/runbooks/artifact-storage.md.

Details: docs/features/infrastructure.md → *The app on prod*.

---

## 2026-09-10 — Flashing from the browser: offsets, ordering and one more `ff_cfg` writer (R0-fe-3)

- **No offset is ever derived, only read**. Every address the flasher writes comes from
  `GET /v1/agent/manifest` — `builds[].parts[].offset` and `config_partition.offset`. The
  bootloader is at `0x1000` on ESP32 and `0x0` on the RISC-V parts. A hardcoded offset
  flashes cleanly and never boots. This is the most expensive failure this feature can
  have. `planWrite` is the single place a write is constructed.
- **The server mints the enrollment token LAST and revokes it on failure**. Check → manifest →
  chip and flash-size checks → download and sha256-check every part → *then* mint. It is
  a single-use fleet-join credential: minting first spends one on every failed attempt,
  and a live one baked into a half-flashed board is an orphan nobody is tracking. On any
  failure after the mint the flasher revokes it and says which id, never the plaintext.
- **The form re-runs the engine's own validation on every keystroke**. Not redundancy:
  `power=sleepy` with no wake interval `POST /v1/enroll` refuses it (422), and reaching
  that refusal costs a token. The Flash button is dead until the config would be accepted.
- **A third implementation of `ff_cfg` needed a shared golden vector**. `ff_cfg.py`
  (writer), `ff_cfg.c` (firmware reader) and now `frontend/src/ffcfg.ts` (browser writer)
  must agree byte for byte. `frontend/src/ffcfg.vector.json` carries fields plus the
  sha256 the **Python** writer produced for them, and both suites assert it from their own
  side, so neither writer can move alone. The vector is ASCII-only — `json.dumps` defaults
  to `ensure_ascii=True` and `JSON.stringify` does not. That is the only region where
  the two are guaranteed to produce identical bytes.
- **`explainFlashError` belongs to the seam, not the adapter**. The commonest failure of
  all is `requestPort()` rejecting because the operator dismissed the chooser — thrown
  *before* an adapter object exists. Thus, it the engine catches it, which must not import
  esptool-js. Found in T2 against a real Chromium, where the page said `Failed to execute
  'requestPort' on 'Serial': No port selected by the user.` instead of "No board
  selected.". Related: `DOMException instanceof Error` is **true** in Chromium and
  **false** across realms (jsdom's). Thus, the translator reads `name`/`message` off the
  value rather than testing `instanceof`.
- **Erase-on-by-default is a correctness setting**. A re-flashed board whose NVS still
  holds a broker credential reuses it (R0-fw-1: "reusing the stored credential"). Thus, the
  freshly minted token baked into it is never spent and the board never re-registers.
- **`flashSize: 'keep'` costs us esptool-js's fit check, so we do it ourselves**. All three
  of `flashMode`/`flashFreq`/`flashSize` are `'keep'` to stop esptool-js rewriting the
  bootloader's flash-parameter byte and recomputing the image SHA. The price is that a
  2 MB board would silently accept the 4 MB A/B layout. `checkFlashable` derives the bound
  from the manifest — never the string `4MB`, never `ab-4m-v1`.
- **No MD5 read-back.** esptool-js can check flash contents with MD5. Web Crypto has no
  MD5 and pulling in `crypto-js` to get one is the wrong trade. The sha256 of every part checks against the manifest *before* the first byte is written, which catches the
  failure that actually happens (a truncated download), and esptool-js checksums every
  block on the wire.
- **esptool-js 0.6.1 has no `romBaudrate` option** (the plan assumed one). It has a fix at
  115200 inside `ESPLoader`. Tutorials that pass one target a different major. `baudrate`
  is what the stub raises the link to after connecting.
- **The dev container's `node_modules` volume does not re-seed itself**. Adding
  `esptool-js` to `package.json` is invisible to the running Vite server until the named
  volume disappears (`docker compose stop frontend && docker compose rm -f frontend &&
  docker volume rm fleetforge_ff_node_modules && just up`) — otherwise the browser gets
  "Failed to resolve import", which reads like a code bug.

---

## 2026-09-10 — The live device list, and why a stream is not a source of truth (R0-fe-2)

- **The dashboard never patches a row from an event payload**. `GET /v1/devices` is the
  record. A frame on `/v1/events` only says "go re-read". The payload carries an `online`
  snapshot and using it is the obvious free optimisation. It is also how the table starts
  disagreeing with the server about a board. The frame parses only far enough to reject
  garbage, and is never rendered or logged.
- **The plain 10 s re-read is a correctness requirement, not a fallback**. A sleepy board
  goes offline with **no event at all** (presence expires on read, `2.5 × wake`, and
  publishes nothing). A purely event-driven list shows it as online forever and passes
  every test one would naturally write. `fleet.test.tsx` carries the test that fails if
  the interval disappears, and T2 proved it end to end: after the will, the event stream
  showed only `: keepalive`, and the row still flipped 29 s later.
- **`EventSource` is not reliable to notice a dead stream**. Found in T2, not review:
  behind the Vite dev proxy, `docker compose stop api` leaves the socket open and
  `onerror` never fires — the page said `Live` at a stream that was gone and did not
  recover when the api came back. Fix: the read model is the detector. A failed poll marks
  the stream suspect. The next successful read discards the source and rebuilds it. This
  is also the only reconnect path that survives a proxy holding a half-open socket.
- **CLOSED is not CONNECTING**. A network blip leaves `EventSource` CONNECTING and the
  browser owns the retry (`retry: 2000` from the server). A non-200 (401, or
  `sse_max_clients` 503) leaves it permanently CLOSED and needs a manual reconnect,
  1 s → 30 s, deliberately mirroring `RECONNECT_INITIAL_DELAY`/`RECONNECT_MAX_DELAY` in
  `api/eventstream.py`. Treating the two alike either hammers the API or hangs forever.
- **A dead API is not a dead session**. Only a 401 renders the login form. A transport
  failure keeps the last list under a banner. Extends the R0-fe-1 rule to a long-lived
  connection, where the temptation is stronger. This is because the failure is continuous.
- **`just now` must be narrower than the heartbeat interval**. A 5 s "just now" bucket
  exactly swallowed the 5 s heartbeat: last-seen never moved. A frozen column reads
  exactly like a page that stopped updating. Single seconds, pinned by
  `format.test.ts`. Caught by running AC1, not by any unit test written before it.
- **The test seam is an injectable `EventSourceFactory`, because jsdom has no
  `EventSource`**. A structural interface a real `EventSource` satisfies without a cast —
  no polyfill, no new npm dependency. The fake can drive `readyState` 0 versus 2. This is
  the distinction above.
- **`just frontend-test` is separate from `just frontend-build` and both are in `just
  build`**. Vitest config lives in `frontend/vitest.config.ts` because the frontend
  container never reads that file (see R0-fe-1). `npm run build` thus cannot run the
  tests. Thus, the pipeline runs them itself.
- **Details:** `docs/features/enrollment.md` → *Live device list (R0-fe-2)*.
  `frontend/src/fleet.ts` (the refresh engine and the three triggers).
  `.claude/plans/R0-fe-2-live-device-list-sse.md` (the plan).

---

## 2026-09-10 — What the prod box can actually hold (R0-infra-4)

- **Swap *used* is a stock, not a flow**. The TODO's "already swapping ~1 G" was a point
  sample. It was already wrong (the box rebooted and swap-used dropped to 11 MB by the
  next measurement). A gigabyte of cold anonymous pages parked in swap and never read back
  costs nothing. What costs is the *rate* of `pswpin` / `pgmajfault`. A verdict built on
  "swap used is 1 G, thus resize" would be wrong. A verdict built on window deltas
  over 15 minutes is defensible. This is the reusable insight.
- **`memory.events max` is the real under-provisioning signal**. It counts forced reclaims
  *at* the limit — which happen long before an OOM kill and are otherwise invisible. A
  container with `max > 0` is under-provisioned even if it never crashes. `memory.peak`
  alone is not enough. `docker stats` and `memory.current` both include reclaimable page
  cache. The harness reads cgroup v2 directly and reports both.
- **Measuring on dev in the production shape transfers**. Fleetforge's app is not on prod
  yet (R0-infra-5 waits on permissions). Thus, the measurement splits: footprint of api /
  ingestor / frontend → dev box in the production shape (`just up-prod`: built images,
  nginx not Vite, same limits). Host headroom → `prod` over a sustained window. Container
  RSS for these workloads is set by the workload, not the host. The projection is then:
  measured prod headroom − measured fleetforge footprint − margin. It is a projection and
  must say so. The harness is the acceptance instrument for R0-infra-5 to confirm it.
- **The shared-Postgres cost must be counted explicitly**. Fleetforge on prod does not
  bring its own Postgres — it adds a database and connections to the shared `postgres-prod`
  (1 GiB limit, 199 MiB in use pre-fleetforge). Every backend is ~5–10 MB of private RSS:
  api pool connections × api processes, plus one dedicated `LISTEN` connection per API
  process (`application_name='fleetforge-events'`, from R0-be-5), plus the ingestor's pool.
  This is not in the 512 M declared-limit table and must be measured directly rather than
  estimated.
- **Verdict for R0: no resize needed**. The 151 MiB measured footprint (131 MiB app + ~20 MiB
  marginal Postgres) fits in the 384 MiB headroom bingo freed. Declared over-commit is
  99.5% (3904 / 3924 MiB MemTotal), but measured peaks are what matter and the net add is
  negative. Follow-up: re-run `just capacity-check-prod` after R0-infra-5 lands to confirm
  this projection against live measurements.
- **Details:** `docs/runbooks/capacity.md` (how to re-run it, what the numbers mean, the
  resize procedure). `design/production.md` → *Capacity — Measured 2026-09-10* (replaces
  the stale sample with the as-built measurement + verdict).
  `.claude/plans/R0-infra-4-prod-capacity-check.md` (the plan).

---

## 2026-09-09 — The connect-only agent, and how it is proved without hardware (R0-fw-1)

- **`ff_cfg` is a CRC-headered JSON blob, not a struct**. 16-byte little-endian header
  (`FFCF`, version, reserved, payload length, CRC32) followed by compact UTF-8 JSON,
  0xFF-filled to the 4 KB partition reserved by R0-infra-2. A packed C struct would be a
  second wire format to version. The flasher that writes it is a **browser**
  (R0-fe-3) — JSON is the one encoding both ends already have. The CRC is what turns a
  half-written partition into one refusal line instead of a board that connects
  somewhere unexpected. `agent/tools/ff_cfg.py` and `agent/main/ff_cfg.c` are the two
  ends of that contract and `tests/test_ff_cfg.py` holds them together (it greps the C
  source for every key the Python writer emits).
- **The config keys are `ssid`/`psk`/`mqtt_pass`. The spelling is not cosmetic**.
  `tests/test_agent_partitions.py::test_agent_holds_no_credential` fails the build if
  anything under `agent/` puts `wifi_password`, `mqtt_password` or an `ffe_…` literal
  next to a quoted value. That tripwire is worth more than pretty names. Thus, the names
  moved. Where a long name was unavoidable (`ff_enroll.c` parsing the enroll response)
  the literal is split (`"mqtt_" "password"`) with a comment saying why it must not
  be "tidied".
- **`ff_net` is a seam with two adapters. That is what makes a hardware-free T2
  possible**. `ff_net_wifi.c` is what ships on a board. `ff_net_openeth.c` drives QEMU's
  OpenCores NIC and compiles to a refusal stub wherever `CONFIG_ETH_USE_OPENETH` is off.
  `link` in `ff_cfg` picks one at runtime. Same idiom as the local/GCP seams in the
  Python side.
- **`CONFIG_ETH_USE_OPENETH=y` lives in `sdkconfig.defaults.esp32` only**. The emulated
  NIC exists on no real board and on no other target. The common defaults file stays the
  one place the safety posture comes from.
- **`CONFIG_MBEDTLS_HAVE_TIME_DATE=y` — the one that was silently missing**. ESP-IDF
  defaults it **off**, and with it off mbedTLS never looks at `notBefore`/`notAfter`: an
  expired certificate checks, and `spec/device-protocol.md` → *Clock — SNTP before
  TLS* describes a failure that cannot happen. Found by AC5 doing the opposite of what
  the plan predicted — a board with a 1970 clock completed a real TLS handshake against
  `bingo.tvaroska.sk`. Enabling it makes the spec's rule true, and costs a board whose
  SNTP never answers its TLS channels (accepted, the SNTP client keeps retrying in the
  background while the enroll ladder waits). `verify_bundle.py` and
  `test_agent_partitions.py` both require it now, in the **resolved** config.
- **`CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y`, no pinned CA**. Both channels stop at
  Traefik with a Let us Encrypt certificate (R0-infra-3), so the Mozilla root bundle is
  the trust store. Proved in QEMU against the real production hostname.
- **The confirm call has a guard on `ESP_OTA_IMG_PENDING_VERIFY`, and R0 must not "fix"
  that**. `esp_ota_mark_app_valid_cancel_rollback()` runs only for an image the
  bootloader is actually watching. A serially flashed board never enters that state. Thus,
  the timer is inert today. Calling it unconditionally at boot would compile, look
  correct, pass every R0 test — and disable R2's auto-rollback on the entire fleet.
- **No goodbye publish**. A board that is dying cannot send one. Presence-off is the
  broker's job via a retained LWT. This is what `/v1/devices` and the dashboard already
  key off (R0-test-1 established the same posture for the simulator, the simulator sends
  a goodbye because it is a process, not a board).
- **The token stays in `ff_cfg` after it is spent**. Erasing it would mean writing to a
  partition the firmware otherwise only reads, on every first boot, to delete a string
  that is already dead server-side. The credential in NVS is what stops a second
  enrollment, and `ff_store_load()` distinguishes *absent* from *corrupt* so a torn write
  parks the board instead of burning another token.
- **QEMU's eFuse MAC is all zeros. Thus, every emulated board is `000000000000`**. The agent
  warns once per boot and does not paper over it. `device_id` is the eFuse MAC on real
  silicon and there is no special case anywhere in the code. Two emulators are one device
  as far as the fleet is concerned.
- **`.qemu/` is a credential directory, not a cache**. 0700, gitignored: `ff_cfg.bin`
  holds a live single-use token and `flash-esp32.bin` holds, inside NVS, the broker
  password that board was issued. QEMU writes the image back (`if=mtd`). This is exactly
  what makes "a reboot burns no second token" testable — and what makes `--fresh` a
  credential deletion.
- **Gotcha, ~30 min:** the dev stack routes by `Host`, and the emulated board addresses
  this box as slirp's `10.0.2.2` — so every enroll came back **404 from Traefik**, having
  never reached the API, which reads exactly like a firmware bug. Fixed with a dev-only
  `ff-qemu` router in `docker-compose.override.yml` (and `10.0.2.2` added to Vite's
  `allowedHosts`, which rejects unknown Hosts for the same reason).
- **Gotcha:** `just` drops empty arguments when it splices `*args` into a recipe, so
  `--ntp ''` cannot be expressed through `just agent-cfg`. "No NTP" is thus a flag
  (`--no-ntp`) — a test knob for the clock rule, not a setting.
- Details: `docs/runbooks/agent-qemu.md` (worked transcript), `docs/features/enrollment.md`,
  `.claude/plans/R0-fw-1-esp32-agent-connect-only.md`.

---

## 2026-09-09 — Agent firmware: what is frozen at flash time (R0-infra-2)

- **`ab-4m-v1` is frozen, and it is a three-way contract**. `agent/partitions.csv`
  (`ota_0`/`ota_1` at `0x1E0000`), `spec/device-protocol.md`
  (`"ota_slot_size": 1966080`, `"partition_layout": "ab-4m-v1"`) and
  `tests/test_agent_partitions.py` (which retypes both literally and greps the spec)
  move together or not at all. OTA cannot change a partition table. Thus, a new
  layout is a NEW ID plus a server that understands both — never an edit to this one.
- **No `factory` partition, deliberately**. A factory-only board can never OTA its way
  to an A/B layout. `make_manifest.py` refuses to emit a bundle whose built table has
  one, is missing `ota_1`, or whose slots differ in size.
- **`ff_cfg` (data, subtype `0x40`, 4 KB @ `0x12000`) has a reservation now, defined later**.
  It is where the browser flasher will write the per-board broker URL, Wi-Fi credentials
  and enrollment token. `R0-fw-1`/`R0-fe-3` own the payload format. Reserving the space
  after boards ship is impossible. Thus, it has a reservation before anything ships.
- **Rollback on, eFuses untouched**. `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` is safe to
  enable at R0 because only an OTA'd app enters `PENDING_VERIFY`. A serially flashed one
  never does. Thus, nothing can brick before `R0-fw-1` exists. Anti-rollback, secure boot and
  flash encryption stay **off**: they burn eFuses per board, irreversibly, and there is no
  key-management story yet. `verify_bundle.py` fails the build if any appears enabled in
  the RESOLVED config.
- **Offsets come from ESP-IDF, never typed**. `make_manifest.py` takes them from
  `build/flasher_args.json` by name. The bootloader really is at `0x1000` on ESP32 and
  `0x0` on the RISC-V parts. A hardcoded value flashes cleanly and never boots on half
  the fleet.
- **The ESP-IDF pin is a digest. Bumping it is a decision, not a version bump**. It
  changes the bootloader and app on every board flashed afterwards while fielded boards
  keep the old one. Procedure in docs/runbooks/agent-build.md.
- **Agent bundles are baked into the app image, not routed through `ObjectStore`**. They
  are build outputs that version with the image, identical for every tenant, ~1.2 MB per
  target. The prod GCS credential cannot currently be minted at all
  (`constraints/iam.disableServiceAccountKeyCreation`, docs/runbooks/artifact-storage.md).
  Putting them behind the object store would take a working feature and make it
  unshippable. R1's *user* artifacts still go through `ObjectStore`.
- **`fleetforge-agent-*` images are NOT compose services**. Never add them to
  `PULL_SERVICES`/`APP_SERVICES`: `docker compose pull` fails as a unit (see the
  R0-infra-5 entry below). They exist as a provenance handle. Production gets the bytes
  from the app image.
- **Gotcha, cost ~40 min:** `espressif/idf:v5.5.5` unpacks to **~8.9 GB**, not the ~5.5 GB
  estimated. The pull dies with `failed to register layer: no space left on device`.
  Reclaim with `builder prune -af` / `container prune -f` / `image prune -f` and
  regenerable caches only — **never** `image prune -a`, `system prune -a` or
  `volume prune` on this box, which holds other projects' images and 31 volumes.
- **Gotcha:** matching forbidden sdkconfig options by PREFIX rejects every correct esp32
  build. `CONFIG_SECURE_BOOT_V1_SUPPORTED=y` is a SoC capability symbol, not an
  enablement. Exact names only. A safety check that fails on correct input teaches the
  next person to delete it.
- **Gotcha:** a project-root `sdkconfig` silently overrides `sdkconfig.defaults` from the
  first build onward, so a committed one would ship a bootloader whose posture no longer
  matches the tracked defaults. Gitignored and dockerignored. Builds run in a container.
- **ESP-IDF builds are not byte-reproducible. That is why provenance is in the
  manifest**. `esp_app_desc_t` embeds the compile date/time, so rebuilding the same commit
  with the same pinned toolchain yields a different `app.bin` sha256 (the partition table
  and otadata are stable). A registry pull of a pushed digest IS byte-identical — checked
  push → `rmi` → pull by digest → export → `diff -r`. `CONFIG_APP_REPRODUCIBLE_BUILD=y`
  would fix the rebuild case but changes every binary. Thus, it is a separate decision.
- **Gotcha:** in IDF 5.x `build/config/` holds only generated `.h`/`.cmake`/`.json`
  views. The text sdkconfig is at the project root. Resolve it from
  `project_description.json["config_file"]`. A literal path breaks on the next IDF bump.

## 2026-09-09 — One app image serves both the api and the ingestor (R0-infra-5)

- **Two images, not three**. `fleetforge` runs the api and the ingestor. They are
  the same code with a different `command`, exactly as `docker-compose.yml`
  already builds them from one `fleetforge:dev`. A separate ingestor image would
  rebuild identical layers and give the pair a way to drift in production.
  `fleetforge-frontend` stays separate — different base, different build.
- **`just build` runs the T1 gate before it pushes**. Prod pulls by tag. Thus, a
  broken build reaching the registry is a production defect, not a local one.
- **The frontend image cannot be checked with `nginx -t`.** nginx resolves
  `proxy_pass http://api:8000` at config load. Thus, the syntax check fails with
  "host not found in upstream" anywhere there is no api container. That is the
  same real behavior that forces the prod api service to carry the network alias
  `api`. But it makes `nginx -t` unusable as a standalone gate. `_verify-images`
  asserts the built payload instead (non-empty `index.html`, an `assets/*.js`).
- **The ingestor must never enter `APP_SERVICES`**. `docker rollout` runs two
  copies during the swap. The ingestor is the sole MQTT subscriber
  (design/production.md → *The single-subscriber rule*): two would double every
  telemetry row and split the SSE audience. Same reason mosquitto stays out.
  It goes in `INFRA_SERVICES`, recreated in place.
- **Gotcha:** never add a name to `PULL_SERVICES` before the compose file defines
  it. `docker compose pull` fails as a unit. Thus, an unresolvable ref breaks the
  deploy for every other app on the box.
- The prod fragment half is not shipped — see docs/features/infrastructure.md.

## 2026-09-09 — TLS for the fleet stops at Traefik, not at the broker (R0-infra-3)

- **The shared Traefik owns 8883**. A `mqtt` TCP entrypoint with a
  ``HostSNI(`bingo.tvaroska.sk`)`` router and `tls.certresolver=myresolver`
  stops TLS and forwards **plaintext** to `mosquitto:1883` on an internal
  network. The alternative (TLS passthrough, or certs mounted into the broker)
  would need DNS-01 or a second renewal path for one service. HTTP-01 over :80
  already issues the certificate. The TCP router just reuses it. Nothing about
  ACME changed, and the broker container knows nothing about TLS.
- **`prod/mosquitto/` in the `services` repo is a copy. The copy has a guard**.
  `deploy.sh` only ships `services/prod/`. Thus, the config has to live there. `acl`
  is the entire fleet authz model, so `validate-config.sh` now diffs the two trees
  and fails the deploy on drift. A copy nobody checks is how a stale ACL reaches
  production.
- **Prod broker usernames must not look like device ids**. `ff-admin` /
  `ff-ingestor`, not 12 lowercase hex digits — the `acl_file` patterns key on `%u`.
  `ensure_client` does create → already-exists → `setClientPassword`. Thus, a
  hex-shaped service username could be re-keyed by enrolling that `device_id`.
  This is the R0-sec-1 reviewer note, now honored in `services/prod/.env`.
- **The broker stays out from `docker rollout`**. Rollout runs two copies during
  the swap. Two brokers cannot share the dynsec store or the 1883 bind. New
  `INFRA_SERVICES` list in `deploy.sh` recreates it in place instead.
- **Gotcha, cost ~25 min:** with the fleetforge dev stack running on this machine,
  `just deploy` fails its staging gate with a **504 on every smoke test while all
  containers report healthy**. The staging Traefik sees the host daemon through
  socket-proxy and picks up `fleetforge-frontend`'s ``Host(`localhost`)`` rule,
  which outranks staging's `PathPrefix(/)`, then cannot reach that network. It
  reads exactly like a content-api regression. `docker compose stop` in fleetforge
  first. See docs/features/infrastructure.md → *R0-infra-3*.

## 2026-09-09 — The dashboard trusts the server's token status (R0-fe-1)

- **The frontend never recomputes token state**. `status` comes from the API, which derives it
  from the burn predicate itself (`auth/enrollment.token_status`). Recomputing it in the browser
  from `expires_at`/`used_at`/`revoked_at` looks trivial and is the bug: the two implementations
  drift. The dashboard eventually shows "active" for a token that `POST /v1/enroll` will
  refuse — which reads, in the field, as broken enrollment rather than a stale token.
- **The issued plaintext lives in React state and nowhere else**. No `localStorage`, no
  `sessionStorage`, no URL, no error message. The server cannot re-derive it, so persisting it
  "for convenience" would be storing an un-rotatable fleet-join credential in the most readable
  place in the browser. Three tests in `EnrollBoard.test.tsx` and one browser-context assertion
  exist purely to fail if this regresses.
- **R0-fe-1 had to ship the login gate**. The task line says "generate token", but the token
  endpoints are admin-authenticated (R0-be-1). Thus, the page was unreachable in a browser without
  one. Scope grew by a screen. The alternative was a page only `curl` could use.
- **Session state is not mirrored client-side**. The cookie is HttpOnly. Thus, the page cannot read
  it. "Signed in?" is `GET /v1/auth/me` plus a 401 watch on every later call. A mirrored boolean
  would only ever disagree with the cookie. A transport error is explicitly not treated as a
  logout. That distinction stops an operator re-typing the admin password at a dead API.
- **Gotcha, cost ~15 min: `vite.config.ts` is loaded by the container. Thus, it can not import test
  deps**. Putting the `test` block there (via `defineConfig` from `vitest/config`) broke the
  `frontend` service with `ERR_MODULE_NOT_FOUND`, because the image's `node_modules` has no
  `vitest`. Traefik then dropped the unhealthy backend and every `/v1/*` call returned a bare
  **404** — a symptom that points at routing, not at a config import. Vitest config now lives in
  `frontend/vitest.config.ts`, which the container never reads.

## 2026-09-09 — The simulator is a device, not a test fixture (R0-test-1)

- **`python -m fleetforge.simulator` imports nothing from the server**. No `fleetforge.config`
  (and thus no mandatory `DATABASE_URL`), no `fleetforge.db`, no `fleetforge.api` — only
  `fleetforge.identity` for `DEVICE_ID_RE`. A simulated board that needs the server's database URL
  to boot is modeling the wrong thing. The import would drag the ORM into a process
  pretending to be an ESP32. This is a deliberate deviation from `broker/__main__.py` and
  `storage/__main__.py`, both of which do `Settings()`. There is an AST tripwire test so the
  deviation cannot rot back.
- **Retain flags are the contract. A wrong one is a silent wrong answer**. `announce` and
  `presence` retained, `hb` **not** — a retained heartbeat would be replayed on every ingestor
  reconnect and `handlers.py` treats a retained message as a replay, so `last_seen` would quietly
  stop advancing. The LWT carries the retain flag too, or a server restart never learns a board is dead.
- **A clean disconnect does not fire the LWT**, so Ctrl-C on an `always_on` board would leave it
  online forever in the dashboard. The simulator publishes a retained `{"online":false}` goodbye on
  any clean shutdown (which is what a planned reboot must do anyway). `--crash-after` uses
  `os._exit(1)` (a TCP FIN with no DISCONNECT) as the *only* honest way to exercise the will.
  aiomqtt has no public API for dropping a connection and `client._client` is private.
- **A sleepy wake is a fresh `aiomqtt.Client`**: entering the same client twice raises
  `MqttReentrantError` (2.5.1). Sleep is a clean disconnect rather than a simulated brownout. The
  server-visible state is identical because `is_online` ignores `presence_reported` for sleepy and
  the ingestor never advances `last_seen` on `presence:false`. Checked live: after the last wake
  the board stayed `online:true` for ~25 s (2.5 × 10 s) and then flipped with **no** message and
  **no** SSE event — presence is computed on read.
- **The device id is a locally-administered pseudo-MAC** (`(sha256(name)[0] & 0xFE) | 0x02`) so a
  simulated board is stable across runs, can never collide with a real Espressif OUI, and can never
  reach the `ffff…` ids `broker/__main__.py` reserves. Provable, and proven in a test, rather than
  unlikely.
- **The broker password goes to `.sim/<device_id>.json` at 0600, gitignored, never logged**.
  It is the NVS analogue and it exists nowhere else — same posture `spec/prd.md` already states for
  a real board. State present means **no enrollment happens**, because tokens are single-use. A
  corrupt state file is a loud error rather than a silent re-enroll that burns one.
- **The server checks everything before the token arrives** — the same ordering rule
  `POST /v1/enroll` follows internally. A `sleepy` board with no wake interval fails before the
  HTTP call, not after the burn (confirmed against a live stack: the token stayed `active`).
- **`fleet` issues its own tokens** through `POST /v1/auth/login` → `POST /v1/enrollment-tokens`,
  because typing three single-use tokens by hand is the friction this task exists to delete. Logs print token **ids**, plaintexts never are.
- **The agent connects with `client_id = device_id` and `clean_session = false`**, implementing the
  proposal R0-be-4 recorded: command durability comes from the persistent session, never from a
  retained `dn/cmd`, and MQTT 3.1.1 requires a non-empty client id for one. Three additive
  `spec/device-protocol.md` changes follow from this work and are **proposed, not written**. The
  LWT publishes with `retain = true`. The agent's client id and clean-session flag. And a
  planned shutdown MUST publish a retained `{"online":false}` before disconnecting.

---

## 2026-09-08 — Broker authz: dynsec authenticates, acl_file authorizes (R0-sec-1)

- **Mosquitto 2.0's dynamic-security plugin does not support `%u`/`%c` substitution**.
  Checked against 2.0.22: a `device` role holding `publishClientSend ff/v1/d/%u/up/#`
  denies the very client it names (MQTT v5 PUBACK 135), while the same role with a literal
  topic allows it. The plugin binary contains no substitution code. `%u` is `acl_file`
  syntax. Every earlier document that says the two pattern ACLs live in a dynsec role (the
  R0-be-4 plan, `broker/provisioner.py`, `CRITICAL.md`'s wording) was wrong about the
  mechanism, not about the model.
- **So the two mechanisms are split: dynsec = authentication (who exists, what password,
  written by `/v1/enroll`), `acl_file` = the fleet ACL (the two `%u` pattern rules,
  verbatim from `spec/device-protocol.md`)**. Mosquitto consults both and **allow wins**,
  proven in both directions. The spec's promise ("no per-device ACL rows, nothing to
  provision at enrollment") survives intact. Only the file it lives in changed.
- **The dynsec `device` role exists and is empty**. `createClient` requires a role name
  (`broker.DEVICE_ROLE`), and a name mismatch answers 503 on every enrollment. Moving the
  pattern rules into it does not fail loudly — it silently denies the whole fleet.
- **Read authorisation runs on delivery, not on SUBSCRIBE**. With `acl_file` a device
  can subscribe to `#` and gets SUBACK 0, then receives only its own `dn/` traffic (checked).
  Any test that asserts on the SUBACK code proves nothing. Same class: a forged LWT passes at CONNECT and drops when it fires, so presence cannot be forged for another
  board (checked — the ingestor logged nothing for the impersonated device).
- **A denied publish is invisible below MQTT v5. The broker log does not help.**
  aiomqtt/paho surface only the local `rc`, and 3.1.1 has no reason code at all. Mosquitto
  2.0.22 logs `Denied PUBLISH` at `MOSQ_LOG_DEBUG`, which `mosquitto.conf` does not enable
  (debug logs every topic — not worth the noise). The authoritative check is
  `mosquitto_pub -V 5 -d` **inside** the broker container, reading the PUBACK. `RC:135` is
  the denial, `RC:0`/`RC:16` are both "allowed" (16 only means nobody was subscribed, so an
  allowed publish reads as `RC:0` whenever the ingestor is up). `just broker-check` asserts
  on non-delivery instead, which is what the Python client can actually see.
- **`dynamic-security.json` is mutable state in the data volume, owned by uid 1883**. The
  plugin rewrites it on every enrollment. A root-owned file logs "not writable", applies the
  change in memory, and loses every device credential at the next restart — with the API
  reporting success. The bootstrap chowns and chmods it, and `docker compose logs mosquitto
  | grep -c "not writable"` is an acceptance check.
- **Bootstrap runs a throwaway broker inside a one-shot init container**. `mosquitto_ctrl
  dynsec init` is the only file-mode subcommand. Everything else needs a live broker. The
  script is idempotent (create → "already exists" → `setClientPassword`). Thus, it is safe on
  every `up`, and the broker `depends_on` it with `service_completed_successfully`.
- **The healthcheck authenticates as the dynsec admin**, the only client that exists before
  the bootstrap and the only one `dynsec init` gives `$SYS` read. The topic must be
  single-quoted (`'$$SYS/broker/uptime'`). A broken broker probe appears as a Traefik 404,
  not as an unhealthy badge.
- **The ingestor gets its own credential and its own read-only role**
  (`subscribePattern` + `publishClientReceive` on `ff/v1/d/+/up/#`, no `$SYS`, no write).
  It is a different privilege from the API's dynsec admin and rotates separately.
- **Devices enrolled during the `NullProvisioner` era cannot reconcile**. The broker
  password only ever existed in the enrollment response. The server cannot re-provision one
  the device would know. They must re-enroll with a fresh token — documented in
  `docs/runbooks/dev-stack.md` rather than built as a command that cannot work.

---

## 2026-09-08 — Object store: one Protocol, two adapters, and the prefix is a security boundary (R0-be-6)

- **One `ObjectStore` Protocol, two real adapters, selected by configuration** — the same
  shape as `fleetforge.broker` (`BrokerProvisioner` / Null / Dynsec). MinIO (S3) in dev
  and for V2 self-hosting, GCS in production. Both SDKs are imported **lazily inside the
  factory**, so neither is on the API's import path and an unconfigured deployment pays
  nothing. Four verbs only — `put`, `get`, `signed_url`, `delete`. **No `list`**: nothing
  in R1 needs it. It is the one verb an IAM prefix condition cannot constrain (see
  below), so adding it would silently widen the grant.
- **`GCS_PREFIX` is a security boundary, not tidiness**. `gs://btvaroska` is *shared*.
  It holds this estate's `.env` backups under `secrets/`, plus the boris podcast audio.
  Object keys arrive from an HTTP request body (R1's upload), so the prefix applies only
  **twice and independently**: `resolve_key()` in-process, and an IAM condition on the
  service account (`resource.name.startsWith(".../objects/fleetforge/")`). Either alone
  is one bug away from writing into `secrets/`.
- **`resolve_key()` rejects, never normalizes** — the rule `identity.py` already
  established for device IDs. `..`, a leading `/`, `//`, backslashes, control or
  non-ASCII bytes, `?`/`#`, over 512 chars: all `ObjectKeyError`, which is a `ValueError`
  and deliberately **not** an `ObjectStoreError`. This is because a bad key is a 400 (the caller
  is wrong) while everything else is a 404 or a 503 (we are). Path normalisation is how
  traversal bugs get written. `a/../../b` has an obvious "sane" reading, and acting on it
  is exactly the mistake.
- **Two S3 endpoints, because a presigned URL signs the `Host` header**.
  `S3_ENDPOINT_URL` (`minio:9000`) is what the API talks to. `S3_PUBLIC_ENDPOINT_URL`
  (`localhost:9000`) is what URLs are *signed against*. This is because the device is not on the
  compose network. Rewriting the host after signing invalidates the signature — there is
  no post-hoc fix. Thus, the split has to exist at signing time. The container selftest
  thus cannot get the URL it prints, and says so instead of failing.
- **Both backends configured is an error, not a precedence rule**. "Which bucket did my
  firmware go to?" must not be answered by reading a factory. Unset one or set
  `OBJECT_STORE_BACKEND`. Likewise **no ADC fallback for GCS**: ADC on a GCE VM carries
  no private key (so no V4 signing) and resolves to the project-wide compute default SA —
  the exact credential the prefix condition exists to prevent. Missing credentials fail
  loudly at construction.
- **Unconfigured is a WARNING plus a 503, never a startup crash**. `create_app()` stays
  constructible with no environment at all (the R0-be-1/R0-be-4 precedent). Artifact
  routes will answer 503 until storage is configured.
- **GCS was never round-tripped against the real service**. `btvaroska` inherits
  `constraints/iam.disableServiceAccountKeyCreation`. Thus, the key the adapter requires
  has no mint path. The keyless alternative needs an IAM grant this task was not
  authorized to make. The SA and its conditional binding exist. The credential does not.
  **Do not read a green dev stack as evidence that production storage works** — the
  options (impersonation + `signBlob`, or a policy exemption) are written up in
  `docs/runbooks/artifact-storage.md`, and one of them is a prerequisite for R1.

## 2026-09-08 — SSE: one listener per worker, and a reconnect ends every stream (R0-be-5)

- **One dedicated asyncpg connection per API process, never a pooled one**. `LISTEN`
  only delivers to a backend that is between transactions, and `pool_pre_ping`/recycle
  would drop the registration with nothing in the log. The symptom is a stream that
  connects and stays empty forever. `db/base.asyncpg_dsn()` converts the SQLAlchemy
  URL. The connection sets `application_name = 'fleetforge-events'` so
  `pg_stat_activity` answers "is anything listening?" without reading code. This is the
  one documented exception to "`get_sessionmaker()` is the only door into the database
  from the API".
- **A listener reconnect closes every SSE stream**. The hub cannot know what was missed
  while the connection was down. A client that keeps reading after a gap silently
  shows a stale fleet. Ending the stream makes `EventSource` reconnect and re-read
  `GET /v1/devices`. This is the same self-healing path as the slow-client case. That
  is also why there is no "resync" event type.
- **A slow client is disconnected, not buffered**. Bounded per-client queues
  (`sse_queue_size`). On overflow the queue is drained and a sentinel ends that one
  stream. Dropping individual events instead would leave a client silently wrong, and
  unbounded buffering is a memory leak in a 256 M container.
- **The NOTIFY payload undergoes a check and then forwarded *verbatim***. `fw_version` comes
  off the wire from a board, and SSE framing is newline-delimited: a payload containing
  a raw newline would let a device inject a forged event into the operator's stream. It
  cannot happen today (`model_dump_json` escapes control characters). That is why it has an assertion rather than assumed. Forwarding the original rather than a re-serialization
  keeps the additive-evolution rule — re-serializing would strip fields a newer ingestor
  adds.
- **Auth undergoes a check once, at connect. Thus, a stream is capped at 15 min**
  (`sse_max_stream_s`). Instant revocation is the reason we rejected JWT (R0-be-1). An unbounded stream would quietly outlive a revoked token. The cap is deliberately
  under nginx's `proxy_read_timeout 3600s`. Browser `EventSource` cannot send an
  `Authorization` header at all. The stream authenticates on the `ff_session` cookie.
  This is R0-be-1's "one credential, two transports" paying for itself. **A token in
  the query string triggered a rejection:** nginx's access-log format logs `$request`.
- **`GET /v1/devices` shipped here, not in R0-fe-2**. `events.py` and `presence.py` both
  already define the contract as "the event is a hint. Re-read `GET /v1/devices`", and
  no task owned that endpoint — an SSE stream whose documented contract is "go read an
  endpoint that 404s" is not a finished artifact. Presence is computed on read via
  `presence.is_online`, with one `now` for the whole response. `presence_reported` is
  deliberately not exposed. Thus, no client can re-derive the rule.
- **Gotcha, and it will bite the next streaming endpoint too:** `httpx`'s
  `ASGITransport` buffers the entire response body before returning, so
  `client.stream()` against an endless SSE generator hangs the whole suite. The tests
  drive the ASGI app directly (`tests/test_events_stream.py::drive_sse`). Only responses
  that never stream (401, 503) go through the normal client. Related: Starlette
  *cancels* the generator on disconnect for ASGI spec_version < 2.4 (uvicorn reports
  2.3), so the subscription is released in a `finally:` inside the generator, not after
  it — anywhere else leaks one subscriber per page reload.
- **`asyncpg.InterfaceError` catches alongside `PostgresError`/`OSError`** in the
  listener's reconnect loop. asyncpg raises it for "connection is complete", which the
  keepalive `SELECT 1` hits when the socket died between two ticks. Letting it escape
  would kill the listener task for the life of the process — the exact silent failure
  this module exists to prevent, with nothing unhealthy anywhere. asyncpg also ships no
  `py.typed`. Thus, it gets one `ignore_missing_imports` override in `pyproject.toml`
  rather than a `# type: ignore` at every call site.
- **No Redis and no broadcaster abstraction**. One backend, and the "no Redis" decision
  is already recorded under *The ingestor is the only MQTT subscriber*. There is no
  second implementation of this boundary and none is planned. Thus, no adapter pair.

---

## 2026-09-08 — Enrollment: commit, then provision. And the grace window (R0-be-4)

- **Check the token secret before calling `BURN_SQL`**. The statement keys on `id`
  alone, and an `ffe_` token's id is not a secret. It is in the issuance response and
  in the api log. Burning before `averify_secret` would let anyone who has read a log
  line destroy every outstanding token: a bench full of boards that will not enroll,
  with the dashboard reporting them `used` and nothing failing loudly. Order is
  `require_admin`'s: parse → row → `dummy_verify` on a miss → check → burn.
- **The handler INSERTs the device row before the burn, in the same transaction**. This is because `enrollment_tokens.used_by_device_id` is a real FK. A refused burn rolls both back.
- **The transaction commits BEFORE the broker is provisioned**. `db/models.py::Device`
  put `broker_provisioned_at` in the schema "so provisioning can be reconciled and
  retried idempotently after a partial enrollment" — the schema already chose this.
  Holding a row lock and a pooled connection across an MQTT round-trip turns a broker
  outage into `idle in transaction` on a 256 M container. The inverse failure (a broker credential for a device that is not enrolled) is prevented by the order,
  not by a transaction.
- **A burned token can be re-presented by the SAME `device_id` for 600 s**
  (`config.enroll_retry_window_s`) and gets a freshly provisioned password. The device
  writes NVS only after it reads the response body. Thus, a dropped packet on first boot
  otherwise leaves a board that is enrolled and has no credential, holding a token that
  can never burn again — a re-flash, in the field. Single use is intact: the lookup
  matches on `used_by_device_id`. Thus, one token still enrolls exactly one board forever.
  `FOR UPDATE` keeps a concurrent revoke from racing it. **PROPOSED for
  `spec/prd.md` → *Security & data posture*** (protected, so not written there): state
  the grace window next to "cannot be replayed from a recovered board".
- **`mqtt_username` is `device_id`, unnormalized**. The `%u` pattern ACLs are the entire
  fleet authz. Thus, the eFuse-MAC format check runs before any credential exists and a
  non-canonical `device_id` triggers a rejection rather than lowercased. `DEVICE_ID_RE` moved to
  `fleetforge/identity.py` — the API must not import from `fleetforge.ingestor`, same
  precedent as `clock.py` leaving `api/deps.py`.
- **`NullProvisioner` leaves `broker_provisioned_at` NULL on purpose**. The dev broker
  is anonymous until `R0-sec-1`. `WHERE broker_provisioned_at IS NULL` is then the
  honest reconcile list rather than a column that lies. Selection is by the presence of
  `MQTT_DYNSEC_USERNAME`/`_PASSWORD`, with a startup WARNING — the same shape as
  `ADMIN_PASSWORD_HASH`.
- **Dynsec gotchas, all checked against Mosquitto 2.0.22's protocol:** responses come
  back only to the issuing client on `$CONTROL/dynamic-security/v1/response`, so
  subscribe before publishing. An error is a *key in the response body*, not a transport
  failure. `correlationData` is echoed and must be matched, or a stale reply from a
  timed-out command is read as this one's success. The dynsec client id carries a random
  suffix, because two API workers sharing one kick each other off mid-command and the
  symptom is an intermittent 503. `clientid` is deliberately not bound to the credential.
  `spec/device-protocol.md` does not specify the agent's client id and `R0-fw-1` is
  unwritten. **PROPOSED for `spec/device-protocol.md`**: state that the agent connects
  with `client_id = device_id`. This would make that binding free hardening later.
- **`ingestor/store.py` finally has rows to update**. Until this task, nothing in the
  codebase inserted a device. Thus, every published message dropped by design.

---

## 2026-09-08 — Ingest: derived presence, and the retained-replay trap (R0-be-3)

- **`last_seen` advances only on a live message that is not `presence{online:false}`**.
  Retained `announce`/`presence` replay on every ingestor reconnect (the process
  re-`subscribe`s, so the broker re-sends the whole retained set). The LWT comes from the *broker*, not the device. Either one, treated as evidence of life,
  marks a dead fleet alive — and for `sleepy` boards, where "the LWT fires on every
  normal sleep and means nothing", it never self-corrects. MQTT's `retain` flag on
  delivery is the discriminator: set only for a retained replay. Checked live —
  `docker compose restart ingestor` replays `up/presence` with `retain=True` and
  `last_seen` does not move.
- **`last_seen = GREATEST(last_seen, :at)`**. QoS 1 is at-least-once. Monotonicity is
  one SQL function, not a comparison in Python.
- **The ingestor `UPDATE`s and never `INSERT`s**. The only way into the registry is a
  burned enrollment token (R0-be-4). An `INSERT … ON CONFLICT` here would make anyone
  who can publish to the broker a fleet member. The dev broker is anonymous today, so
  `ingestor/store.py` is the file that stops it. A decommissioned device drops by
  the same `WHERE`, with a log line, rather than resurrecting its row.
- **`pg_notify` runs in the write's transaction, via `SELECT pg_notify(:channel, :payload)`**.
  `NOTIFY` takes no bind parameters. Thus, the string form is an injection with a
  device-controlled payload. And transactional delivery means SSE can never announce a
  row the database does not have. Payload capped at 7500 B against PostgreSQL's 8000 B
  limit, and an oversized event is skipped rather than allowed to fail the write.
  `fleetforge.events` ships `EVENTS_CHANNEL` and `DeviceEvent`. R0-be-5 imports both
  rather than restating either — a channel name spelled twice is a silently empty SSE
  stream with nothing failing loudly.
- **`presence.is_online()` is the single rule, and presence stays uncomputed in the
  database**. The event's `online` is a snapshot for the SSE consumer. The API
  recomputes on read. This is because a sleepy device goes offline with no message arriving at
  all. The 2.5 tolerance lives once, in `config.presence_tolerance`.
- **An announce whose `power_class` would violate a CHECK loses that field, not the
  whole message**. `fw_version` is what tells the operator the OTA landed. Dropping the
  announce over a barely-used field would be the wrong trade. The pair undergoes a check in
  Python, the CHECK stays the backstop.
- **A payload `device_id` that disagrees with the topic drops**. The topic is
  authoritative. It is what the `%u` pattern ACL binds to the broker username.
- **One message never kills the process**. Specific exception families
  (`SQLAlchemyError`, `OSError`, `ValueError`) around the per-message write, and the
  heartbeat file touched even on failure: liveness is broker-connectedness, and
  restarting the container does not fix Postgres. Checked by stopping Postgres under
  load — one ERROR line per message, container still healthy, full recovery on restart.
- **`now_utc()` moved to `fleetforge/clock.py`**. It lived in `api/deps.py`. The
  ingestor must not import `fleetforge.api` — pulling FastAPI's app factory into a
  process with no HTTP server would drag its settings validation along with it.
- **Gotcha fixed in passing:** `just mqtt-pub` wrapped the payload in a double-quoted
  shell word, so the shell ate every `"` in a JSON body and the broker received
  `{proto:1,…}`. It presents as a `JSONDecodeError` from the ingestor and looks like an
  ingest bug. The payload now travels in the environment. The recipe also takes a
  `retain` argument, since retained state is most of what this task had to be tested
  against.
- **PROPOSED for `spec/` (protected, so not written there):** `spec/device-protocol.md`
  → *Open items for R0* asks whether `up/log` ships in R0 — the answer this task
  implements is "accepted and dropped. It only moves `last_seen`, storage is R3".

---

## 2026-09-08 — Enrollment token issuance: one predicate, two readers (R0-be-2)

- **`BURN_SQL` ships as an importable constant** in `fleetforge.auth.enrollment`, not
  as prose to copy. `R0-be-4` imports it, and `tests/test_invariants.py`'s four burn
  tests (including the two-connection race) now exercise the shipped statement
  rather than a duplicate of it. Supersedes the "R0-be-4 must copy this verbatim"
  instruction in the `EnrollmentToken` docstring and `R0-db-1` §12.
- **The API's derived `status` is *defined* as the burn predicate** (`active` iff the
  burn would succeed) and there is a parametrized equivalence test over all four
  states so the two cannot drift. A dashboard that says "active" about a token the
  burn rejects sends someone to the bench with a board that will not enroll. The
  database clock stays the authority. `token_status()` is a show value and never an
  authorization decision. This is always the conditional UPDATE.
- **The plaintext is in the `POST` response body on purpose**, unlike the admin login
  token. A human copies it into the flasher's baked config (`spec/flows.md` Flow 1), so
  it must be readable exactly once. It is never logged (ids only), never re-derivable.
  The list response model has no field that could carry it.
- **24 h lives in `config.enrollment_token_ttl_hours` and nowhere else** — no DB
  default, no per-request override. The number's home is `spec/prd.md`. A `ttl_hours`
  in the request body would be a second place the rule can be violated.
- **Revoke is `POST …/revoke`, not `DELETE …`**. Revoked rows are retained 90 days
  (`spec/prd.md` → *Retention*) and `used_by_device_id` is the fleet's enrollment
  provenance. A `DELETE` verb would invite someone to actually delete it. Revocation is
  the same conditional-UPDATE idiom as admin-token revocation. Thus, a second call is
  idempotent rather than a moved timestamp.
- **Group CRUD deliberately does not exist**. Tokens are group-scoped and the schema
  supports it. But nothing creates or lists `device_groups`, so R0 tokens are ungrouped
  in practice. That is correct for R0 (bulk deploy is V3). Flagged as a follow-up task
  rather than smuggled in.
- **`bearer_scheme` / `cookie_scheme` moved to `api/deps.py`**. They were private to
  `routers/auth.py`. Every protected router needs them, and one credential deserves one
  declaration. `auto_error=False` on both remains essential — with the default, FastAPI
  403s before `require_admin` runs.
- **PROPOSED for `spec/prd.md` → *Requirements & targets*** (spec has protection): promote
  the enrollment-token TTL from the PROPOSED prose in *Security & data posture* into the
  *Timing* table as **enrollment token lifetime = 24 h**. Thus, it sits with the other
  numbers code resolves against.

---

## 2026-09-08 — Admin auth: one credential, two transports (R0-be-1)

- **One credential type**. The login cookie carries *the same* `ffa_` token a CLI
  would send in `Authorization: Bearer`, checked by one code path
  (`api/deps.py::require_admin`). There is no session table and no second credential
  kind, so revoking a dashboard session is the same single `UPDATE` as revoking a
  CLI token. Confirms `design/architecture.md` → *v1 admin auth*.
- **Argon2id pinned to `t=2, m=19 MiB, p=1` behind an `anyio.CapacityLimiter(2)`**.
  The library defaults (64 MiB, and Starlette's 40-thread threadpool) would peak
  around 760 MiB inside a 256 M container — an OOM kill under concurrent logins.
  Verification reads the parameters out of the stored PHC string. Thus, the profile can
  change later without invalidating existing hashes.
- **The verification cache memoizes the hash comparison only**. The row is read and
  `revoked_at` / `expires_at` re-checked on **every** request. Only the ~40 ms argon2
  comparison is skipped, keyed by `(token_id, sha256(secret))` for 60 s. Caching an
  `AuthContext` instead would silently break instant revocation. This is the entire
  reason we rejected JWT. Checks run parse → row → revoked/expired → check. Thus, a
  revoked token also cannot burn CPU.
- **`ADMIN_PASSWORD_HASH` holds the hash, never the password, and must be
  SINGLE-QUOTED in `.env`**. Checked empirically: unquoted, docker compose
  interpolates the `$argon2id` / `$v` / `$m` segments away and the container receives
  `=19=19456`. The failure mode is a login that can never succeed and a log line that
  does not say why. `python-dotenv` strips the quotes, so one quoted line serves both
  the host process and compose interpolation. `docker-compose.yml` uses
  `${ADMIN_PASSWORD_HASH:?…}` with **no default** — a shipped default admin
  credential is worse than a stack that refuses to boot.
- **`Secure` is unconditional**. `http://localhost` is a secure context. Thus, there is
  no dev/prod cookie switch for anyone to flip in production. Cookie attributes are
  `HttpOnly; Secure; SameSite=Strict; Path=/`, set and cleared identically. No CSRF
  token: the dashboard is same-origin by construction. This is also why CORS
  middleware must never appear. Rejected `__Host-`: no subdomains, `Path=/` and
  `Secure` already fixed, and inconsistent browser behavior over `http://localhost`.
- **Login rate limiting is per-process** (one uvicorn worker per container), keyed on
  the leftmost `X-Forwarded-For` entry with a **global backstop bucket**, because
  Traefik appends to that header rather than replacing it and the key is thus
  client-spoofable. The limiter counts only failures, and the limiter checks both buckets before
  any argon2 work.
- **`db/base.get_session()` deleted**. It called the `lru_cache`d
  `get_sessionmaker()` directly, so `dependency_overrides[get_sessionmaker]` did not
  affect it and a test would have quietly used the developer's dev database. **All**
  API database access goes through `Depends(get_sessionmaker)`. Supersedes the
  hand-off note in `.claude/plans/R0-db-1-schema.md` §12.
- **PROPOSED for `spec/prd.md` → *Requirements & targets*** (spec has protection, so
  these are not written there): session/cookie lifetime **7 days**. Login rate limit
  **5 failures / 60 s per client IP, 30 / 60 s global**. Argon2id profile
  **t=2, m=19 MiB, p=1**.

---

## 2026-09-08 — The standalone Compose stack is the dev environment (R0-infra-1)

- **The stack is both the dev loop and the V2 self-host artifact. It is the
  default dev environment specifically so it cannot rot**. `spec/prd.md` promises
  "ships as one Docker Compose stack" while production is a *fragment* of a shared
  stack. The only way both stay true is to use the whole thing every day.
  `just up-prod` (base compose only: built images, nginx, no bind mounts, no
  `--reload`) is the guard that the production-shaped path still builds. It is
  meant to be run before every commit.
- **One image, two commands**. A single root `Dockerfile`. `api` and `ingestor` are
  the same image with a different `command:`. Confirms `design/production.md` →
  *Open decisions*.
- **MQTT reaches the broker only through Traefik's `mqtt` entrypoint, even in dev**.
  Mosquitto publishes no host port. Thus, the dev path and the prod path are the same
  path. Dev uses ``HostSNI(`*`)`` with no TLS. Prod (`R0-infra-3`) uses
  ``HostSNI(`bingo.tvaroska.sk`)`` + `tls.certresolver` and forwards plaintext
  internally. Traefik thus joins the `backend` network here, which the shared
  Traefik in `services/prod` does not yet do.
- **The API is not routed by Traefik at all.** nginx in the frontend container owns
  `/v1` on the dashboard's origin. This makes "no CORS" structural rather than
  configured. `tests/test_api_health.py::test_no_cors_headers` exists so that a
  future "quick CORS fix" fails loudly. A browser CORS error against this app means
  the nginx proxy is wrong.
- **Dev-only anonymous broker access is quarantined** in
  `mosquitto/conf.d/10-dev-anonymous.conf`, the single file `R0-sec-1` deletes.
  Nothing in `mosquitto.conf` grants or restricts topic access, so the broker's
  security posture is a directory listing rather than a config audit.
- **`.env` is the HOST configuration and is never `env_file:`d into a container**.
  It holds `localhost:5433` for alembic/pytest/just. Containers get
  `postgres:5432` set explicitly. Compose still reads `.env` for `${VAR}`
  interpolation. pydantic-settings gives real environment variables precedence over
  `.env`. Thus, an `env_file:` here would silently point the API at its own namespace.
- **TLS is deliberately absent**. `http://localhost` is a secure context, so Web
  Serial (`R0-fe-3`) and `Secure` cookies (`R0-be-1`) both work. V2's TLS problem remains unsolved but unobstructed (a commented ACME block in the Traefik command and
  an `FF_ACME_EMAIL` placeholder).
- **Gotchas learned, all of which cost time:** (1) the mosquitto CLI clients force
  TLS whenever the port is 8883 and cannot be talked out of it. Thus, the plaintext dev
  broker on the prod-parity port must be exercised with paho — `just mqtt-pub` /
  `just mqtt-sub` exist for exactly this, and the failure mode (`Protocol error`)
  looks like a broken TCP router. (2) **Traefik silently skips containers that are
  not `healthy`**. Thus, a broken healthcheck presents as a 404 from the entrypoint,
  not as an unhealthy badge. `node:22-slim` has neither `wget` nor `curl`, and nginx
  listens on IPv4 only, so container probes must use `127.0.0.1`, never `localhost`.
  (3) The production image does not chown `/app` to the runtime user. The code is
  root-owned and read-only to `appuser`.

---

## 2026-09-08 — Schema, and the conventions the codebase inherits (R0-db-1)

The first code in the repo, so these are settled for everything after it.

- **Spelling is `enrollment` / `enroll` (US), everywhere**. `spec/device-protocol.md` is
  the near-frozen wire contract and it says `POST /v1/enroll`. `TODO.md` and
  `design/architecture.md` say `/v1/enrol`. The spec wins. **Proposed correction:** fix
  those two documents to `/v1/enroll` as part of R0-be-4, which owns the endpoint.
- **No PostgreSQL ENUM types**. The server must tolerate agents it cannot update
  (`spec/device-protocol.md` → *Evolution rules*). A PG enum needs a migration before
  it can store a value a future agent invents — an ingest that raises on an unknown
  `link_type` or `state` is a silent fleet-visibility outage. Vocabulary lives in Python
  `StrEnum`s. The columns are `TEXT`. **Sole exception:** `devices.power_class` has a
  CHECK, because derived presence is only *defined* for `always_on` / `sleepy`.
- **Token wire format is `{prefix}_{uuid-hex}.{secret-b64url}`** (`ffa_` admin, `ffe_`
  enrollment). Argon2id hashes are salted and thus not searchable. Thus, the row's UUID
  must ride in the token as the indexed lookup key. Only the secret half checks against `secret_hash`. Plaintext is never stored.
- **The enrollment burn is one conditional `UPDATE … RETURNING`**, correct under
  PostgreSQL's default READ COMMITTED. Zero rows back means already burned/revoked/expired.
  Never SELECT → check → UPDATE. Statement is in the `EnrollmentToken` docstring and
  proven by a two-connection race test.
- **Devices soft-delete (`decommissioned_at`) and `deploy_events.device_id` is
  ON DELETE RESTRICT**. `deploy_events` stays forever ("the metric history is the
  product's evidence") while every device must stay removable. RESTRICT makes destroying
  KPI history impossible rather than merely discouraged.
- **`devices.device_id` (eFuse MAC, `^[0-9a-f]{12}$`) is the natural PK**. This is because it is
  also the MQTT username the two `%u` pattern ACLs depend on. The format CHECK is a
  security control, not tidiness.
- **Layout: `src/fleetforge/`, uv, SQLAlchemy 2.0 async + asyncpg, Alembic revisions
  `NNNN_slug`, ruff + mypy, pytest against a real Postgres migrated by Alembic**. One
  package because one image ships two entrypoints (`api`, `ingestor`). Dev Postgres
  publishes **5433** — 5432 on this host belongs to an unrelated container.
- **`deploy_events` is deliberately not a Timescale hypertable:** forever retention, tiny
  volume, and an outgoing FK. R3's telemetry table is the hypertable case.

---

## 2026-09-08 — Bingo retirement completed (R0-infra-0)

- **Decision:** Bingo deployment fully retired from production. Containers stopped and
  deleted, database backed up to `gs://btvaroska/retired/bingo/` then dropped, all
  deployment scripts and runbooks updated. Domain `bingo.tvaroska.sk` now free for
  fleetforge. Repository and Artifact Registry images intentionally kept as historical
  artifacts.
- **Why:** Freed 384 MB of declared container limits on a host swapping ~1 GB. Fleetforge
  needs ~512 MB, so net addition is ~128 MB. Also freed the domain with existing Let us
  Encrypt cert (kept to prevent fresh ACME challenge).
- **Gotchas learned:** (1) Deleting services from docker-compose.yml does not stop running
  containers - must explicitly stop before deploy. (2) Smoke tests must be updated in same
  commit that deletes services to prevent deploy auto-rollback. (3) Init scripts are inert
  on existing volumes - database drop requires explicit `DROP` commands. (4) Found
  `prod/bingo.env` tracked in git despite being in `.gitignore` (gitignore does not apply
  to already-tracked files) - filed as separate security task for other tracked env files.
- **Verification:** Post-retirement checks confirmed container count 12→10, memory freed,
  Traefik route 404, database/role dropped with backup checked restorable, full deploy
  pipeline green, other services unaffected.
- **Task:** R0-infra-0 completed 2026-09-08. Details in
  [docs/features/infrastructure.md](docs/features/infrastructure.md).

---

## 2026-09-08 — Adopted gen-3 planning layout

- **Decision:** Migrated from `PLAN.md` + `docs/` to the gen-3 layout used by every
  other repo in the estate: `TODO.md` (live status only), `spec/` (the WHAT,
  status-free), `design/` (the HOW, status-free), `docs/` (planning/ops), this file,
  and `CRITICAL.md`.
- **Moves:** `docs/SPEC.md` → `spec/prd.md`. `docs/device-protocol.md` →
  `spec/device-protocol.md`. `docs/FLOWS.md` → `spec/flows.md`. `docs/DESIGN.md` →
  `design/architecture.md`. `docs/architecture.md` → `design/production.md`.
  `docs/RELEASES.md` → `docs/releases.md`. `PLAN.md` → `TODO.md` (task IDs lowercased,
  `R0-BE-1` → `R0-be-1`, tables → checkbox items).
- **Why:** fleetforge was the last repo on the old layout, and root `CLAUDE.md` still
  documented it. The restructure done the same day had already rebuilt gen-3's
  *distinctions* (requirements versus design versus tasks) under the old filenames. Thus, the
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
  boundary is what keeps deleting it a configuration change. Recorded honestly in
  `spec/prd.md` → *Security & data posture*.
- **Details:** [design/production.md](design/production.md) → *Artifact storage*.

---

## 2026-09-08 — Retire bingo. Fleetforge takes `bingo.tvaroska.sk`

- **Decision:** The unfinished bingo app retires from production and fleetforge reuses
  its domain. Not a public product until V3, so the domain is an operational detail.
- **Why:** frees 384 M of declared container limits on a box already swapping ~1 G, plus
  an existing Let us Encrypt route. Fleetforge needs ~512 M, so the net addition is ~128 M.
- **Not decided:** whether to delete the bingo repo or its Artifact Registry images.
  Retiring the deployment is not deleting the project. The bingo **database must be
  backed up before the role drops** — the one irreversible step (`R0-infra-0`).

---

## 2026-09-08 — The ingestor is the only MQTT subscriber

- **Decision:** A single-instance ingestor process is the sole MQTT subscriber. It writes
  to Postgres and `NOTIFY`s. API workers `LISTEN` and distribute over SSE. The API never
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

- **Decision:** `spec/prd.md` gained a *Requirements & targets* table (capacity, timing,
  retention, KPI thresholds) marked **PROPOSED** pending review. Downstream docs resolve
  against it instead of each deciding for themselves.
- **Why:** the doc set specified mechanisms with no numbers. `device-protocol.md` had
  "heartbeat default interval" as an open item — a spec decision leaking into a protocol
  doc. Metrics existed with no thresholds. Thus, they could not fail.
- **Consequence:** exposed three missing tasks, now in R1 — a signed-URL + range download
  endpoint (Flow 2 promised resumable download with nothing to serve it), writing
  `deploy_events` from R1 (or R5 arrives with two KPIs and no history), and enforcing
  retention rather than only ingesting.

---

## 2026-09-08 — Enrollment over HTTPS, not MQTT. Tokens are single-use

- **Decision:** A device exchanges its enrollment token at `POST /v1/enrol` over HTTPS for
  a per-device broker credential, then connects to the broker already credentialed. The
  token burns on use.
- **Why:** the original flow had the device present its token *to the broker*. This would force Mosquitto to authenticate clients it never heard of against a
  group-scoped token — a custom auth plugin bridging broker to control plane. Instead the
  broker only ever sees fully-credentialed clients and its authz collapses to two pattern
  ACLs. The agent already needs an HTTPS client for artifact download. Thus, this is free.
- **Single-use:** a group-scoped token surviving in flash would let anyone with physical
  access to one board enroll arbitrary devices, and on a public-facing broker there is no
  LAN perimeter to hide behind.
- **Details:** [spec/device-protocol.md](spec/device-protocol.md).

---

## 2026-09-08 — The device owns the reboot, and the rollback

- **Decision:** Two authority rules, both device-side. The device decides *when* to apply
  and can sit in `awaiting_safe_window` indefinitely. And the agent arms the confirm timer on
  the device before the reboot, so rollback is the device's decision, never a server
  command.
- **Why:** a drone rebooting mid-flight falls out of the sky, and a board that cannot
  reach the broker is exactly the board that must roll back. It will never receive a
  command telling it to. The server observes and records. It never forces a reboot.
- **Details:** `design/architecture.md` principle 5. `spec/prd.md` → *Scope*.

---

## 2026-09-08 — MQTT is the control plane only

- **Decision:** MQTT carries identity, presence, commands, status and telemetry. **HTTPS
  carries artifact bytes**. MQTT never carries payload.
- **Why:** MQTT has no range requests. Thus, any drop restarts the whole transfer, and the
  broker would buffer the image per subscriber on a group deploy. The `stage` command
  carries a short-lived signed artifact URL instead.
- **Supersedes:** the initial spec's implication that MQTT was the delivery transport.

---

## 2026-09-08 — Flash-time immutables frozen at R0

- **Decision:** The full A/B partition table (`nvs`/`otadata`/`phy_init`/`ota_0`/`ota_1`,
  4 M flash minimum), `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, and the eFuse posture
  (Secure Boot v2 **off**, anti-rollback **off**, flash encryption **off**) ship from the
  very first flash at R0 — even though nothing writes the second slot until R2.
- **Why:** an OTA image writes *into* a partition. It cannot rewrite the partition table.
  The bootloader is the one update with no rollback path. Wrong at R0 means
  physically getting every deployed board — the exact intervention this product exists
  to delete.
- **Consequence:** R5 ships **app-level signature verification**, not Secure Boot v2.
  Secure Boot v2 burns a key digest to eFuse and needs a re-signed bootloader. Thus, it can never work on an already-deployed board. It is post-v1 and new-devices-only.
  The two are not interchangeable.
- **Details:** [design/architecture.md](design/architecture.md) → *Flash-time immutables*.

---

## 2026-09-08 — Version structure: V1 safe OTA, V2 build, V3 swarm

- **Decision:** V1 = R0–R5, ~5 heterogeneous boards, safe OTA. V2 = R6–R10, VCS +
  server-side compile + simulation. V3 = robotic swarm (gateway + drones).
- **Moved out of v1:** groups & bulk deploy (five different builds have nothing to
  bulk-deploy) → V3. The advisory simulation gate → V2/R8.
- **Rejected as v1 scope:** a 100+ node swarm. Aspirational, not v1. What v1 *does* pay
  for is schema and shape only (`link_type` / `power_class` / `parent_device_id`,
  derived presence, device-owned reboots) never speculative machinery.
- **Rule learned:** take what is a schema or config decision. Defer what is machinery.

---

## 2026-09-08 — v1 is a hosted instance. IP-bearing links only

- **Decision:** v1 ships as a single hosted, single-tenant instance on a public domain
  with Let us Encrypt TLS. Turnkey self-hosting is V2. The device contract requires an
  IP-bearing link and TLS, nothing more — never "Wi-Fi".
- **Why hosted:** onboarding friction is the make-or-break risk, and the hard part of
  self-hosting is TLS without public DNS (a local CA the browser *and* the device trust).
  Deferred, not solved — it returns in full at V2.
- **Consequence:** the broker and artifact endpoint are on the public internet from R0,
  so per-device broker credentials, topic ACLs and single-use enrollment tokens are R0
  requirements, not post-v1 hardening. There is no LAN perimeter to revert on.
- **Gateway-mediated non-IP radios** (Zigbee/BLE/LoRa) cannot reach a hosted server at
  all and need an on-site gateway — a second product, deferred to V2+.
