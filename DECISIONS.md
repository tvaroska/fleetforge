# Decisions

Append-only log of product/technical decisions and learnings. Newest first.
Each entry: what was decided, why, and where the details live. Never rewrite
history — supersede an old decision with a new entry that references it.

---

## 2026-10-21 — CUJ-2 E-Ink & Developer Experience: hybrid Git-driven CI/CD (`CI-CD-1`), rotating secrets (`SEC-1`), OTel log/metric export (`OTEL-1` + `R4-fw-3/4`), and Improv BLE Wi-Fi (`WIFI-BLE-1`)

- **Cause.** Managing a battery-powered e-ink project (`CUJ-2`) across rapid single-board bench iteration and multi-board field deployment exposed four high-priority gaps:
  1. Manual browser compile + upload is too slow for daily iteration; operators need Git-driven continuous deployment configured per project (`repo_url`, `track_mode`, `track_ref`, `auto_deploy_target`) plus a way to hold deep sleep on a bench board (`stay_awake`).
  2. E-ink boards authenticate to an external content/weather API whose API key rotates periodically, and reflashing over USB to rotate a secret defeats fleet management.
  3. E-ink boards wake briefly, fetch/render, and return to deep sleep; logs and metrics must drain before sleep and forward to an OpenTelemetry-compatible collector (`OTLP/HTTP`).
  4. Moving an e-ink frame to a new Wi-Fi network (or adding a 2nd/3rd network) when all known APs are out of range must work from a phone or browser without reflashing.
- **Decisions.**
  - **Pillar 1 — Hybrid Git-Driven CI/CD (`CI-CD-1`):**
    - External CI (`GitHub Actions` / `Cloud Build` via `python -m fleetforge.cli push`) compiles and tests the firmware, then uploads `.bin` + git provenance (`repo_url`, `branch`, `commit_sha`, `tag`, `build_url`, `test_passed`) to `POST /v1/artifact`.
    - `projects` gains `repo_url`, `track_mode` (`off | branch | tag | commit`), `track_ref`, `auto_deploy_target` (`off | dev | all`), and `ci_webhook_url`. When an uploaded artifact's provenance has `test_passed != False` and matches the project's tracking rule, the server automatically deploys it to matching live boards (`is_dev = true` when `auto_deploy_target = 'dev'`, or all live project boards when `'all'`).
    - When the operator changes `track_mode`/`track_ref` in the dashboard (`PATCH /v1/projects/{slug}`) or calls `POST /v1/projects/{slug}/deploy`, Fleetforge immediately deploys the newest matching already-uploaded artifact; if no matching artifact has been uploaded yet and `ci_webhook_url` is set, Fleetforge fires a best-effort JSON POST (`{"event": "fleetforge.track_ref_changed", ...}`) to trigger an external CI build.
    - `devices` gains `is_dev` and `stay_awake`, published on retained `ff/v1/d/{device_id}/dn/cfg` (`{"stay_awake": bool, "secrets_rev": int}`). When `stay_awake` is `true`, `ff_is_ready_for_sleep()` / `Fleetforge.isReadyForSleep()` returns `false` so a bench board stays online for rapid CI deploys.
  - **Pillar 2 — Rotating Secrets Without Reflashing (`SEC-1`):**
    - Plaintext secrets never ride MQTT or persist in Mosquitto's retained store. Instead, `dn/cfg` carries only the integer revision doorbell (`secrets_rev`), while encrypted secret values live in PostgreSQL (`project_secrets`, encrypted at rest with Fernet via `SECRET_ENCRYPTION_KEY`, falling back to a key derived from `SIGNING_PRIVATE_KEY_PEM` in dev/test).
    - When a board wakes and observes `dn/cfg.secrets_rev > local_secrets_rev`, it fetches `GET /v1/device-secrets` over HTTPS using `Authorization: Basic base64(device_id:mqtt_password)` (verified against `devices.mqtt_password_hash`, SHA-256 hex backfilled on enroll), writes the key/value pairs to NVS namespace `"ff_sec"`, updates `secrets_rev`, and re-announces `secrets_rev` on `up/announce`. `ff_sec_synced()` holds `ff_is_ready_for_sleep()` false while a pull is pending so a sleepy board never sleeps before rotating its key.
  - **Pillar 3 — Board-to-OTel Observability (`OTEL-1` + `R4-fw-3/4`):**
    - Firmware `ff_log.h` / `ff_log.c` (`ff_logf()` / `Fleetforge.logf()`) buffers up to 32 lines (`<= 200 B` text, monotonic `esp_timer` timestamp) in RAM plus a 4-line magic+CRC32 `RTC_NOINIT_ATTR` crash-surviving ring (`0x46464C47`) recovered on boot before the first MQTT connect. A background task flushes batches (`<= 2048 B`, at most 1 publish per 5 s, QoS 0) to `up/log`, and `ff_log_drained()` gates `ff_is_ready_for_sleep()` so lines logged during a wake cycle flush before `esp_deep_sleep_start()`.
    - `fleetforge-ingestor` (`src/fleetforge/ingestor/otel.py`) exports `up/log` batches (`POST <endpoint>/v1/logs`) and `up/telemetry` batches (`POST <endpoint>/v1/metrics`) as OTLP/HTTP JSON (`service.name = <project_slug>`, `service.version = <fw_version>`, `device.id = <device_id>`), resolved per project (`projects.otel_endpoint` / `projects.otel_headers`) with global fallback (`FF_OTEL_EXPORTER_OTLP_ENDPOINT` / `FF_OTEL_EXPORTER_OTLP_HEADERS`).
  - **Pillar 4 — Multi-Wi-Fi & Improv Wi-Fi over BLE (`WIFI-BLE-1`):**
    - `ff_net_wifi.c` prepends a writable NVS `"ff_net"` ring (up to 4 SSIDs, newest first) ahead of read-only `ff_cfg` SSIDs and deduplicates by SSID.
    - When all known SSIDs fail to associate on boot/wake, `ff_net_wifi.c` starts a bounded 120-second Improv Wi-Fi over BLE GATT window (`00467768-6228-2272-4663-277478268000`, `Fleetforge-<last4mac>`). Provisioned credentials are tested first, persisted to NVS `"ff_net"` via `ff_net_wifi_save_credential()` on IP acquisition, and BLE stops before deep sleep so an unreachable router cannot drain an e-ink battery.
- **Details:** `spec/device-protocol.md`, `spec/cujs.md` (`CUJ-2`), `docs/features/vcs-integration.md`, `docs/features/health-telemetry.md`, `docs/features/projects-and-users.md`, `docs/features/enrollment.md`, migration `alembic/versions/0011_ci_cd_and_device_cfg.py`.

---

## 2026-10-19 — P1 ships by dropping and recreating prod's fleetforge database, not by a backfill migration; agent blobs survive the artifact cleanup (S0-rel-1)

- **Cause.** d822f1b (P1) rewrote `alembic/versions/0001_initial_schema.py` (`projects` with the seeded `default` row, `users`, `project_members`, `project_id NOT NULL` on devices, groups and enrollment tokens) and `0004_artifact_versions.py` (PK `(project_id, target, version)`) in place. Prod runs v0.6.0, whose head is `0008`, so `alembic upgrade head` would apply only `0009` and `0010`, stamp `0010`, and never run the P1 DDL; every project-scoped query would then fail.
- **Decision (owner, 2026-10-19).** Drop and recreate the `fleetforge` database inside the shared prod `postgres` container (`DROP DATABASE fleetforge WITH (FORCE)`, `CREATE DATABASE fleetforge OWNER fleetforge`) and nothing else: the role, the volume and the other databases stay. Delete the dynsec device clients (usernames `^[0-9a-f]{12}$`) over `$CONTROL` against the running broker. Delete the user-artifact blobs. Deploy v0.7.0; the api migrates `0001` → `0010` on the empty database. The superadmin is the existing env admin login (`ADMIN_PASSWORD_HASH`, a userless unpinned `admin` session; no `users` row). Re-enrol the test boards.
- **Deviation from the literal TODO wording.** The TODO says "clear the fleetforge MinIO/GCS artifact prefix". `gs://btvaroska/fleetforge/` is not wiped: agent bundles (the flasher's firmware) and user artifacts share `blobs/sha256/<hex>` (`design/artifacts.md`, `firmware/index.py`), and an agent `app.bin` was uploaded as a user artifact during R1-test-1. A prefix wipe would delete the flasher's agent and break the re-enrolment this refresh needs. Instead only the `artifacts.sha256` (and `builds.outputs`) digests captured from prod's database before the drop are candidates, minus everything reachable from `agent/index.json`: each entry's `manifest_sha256`, each `superseded[].manifest_sha256`, and every `parts[].sha256` of those manifests. `agent/` is never touched. This is the design rule "never deleted while referenced".
- **Why.** Prod holds only test boards. A backfill migration would exist only to preserve data nobody wants. Recreating matches P1's own D3 ("Drop & Recreate DB") in `docs/features/projects-and-users.md`.
- **Rejected.**
  - A backfill `0011`: it duplicates the DDL that `0001`/`0004` already describe.
  - Hand-stamping `alembic_version`, or `downgrade base` then `upgrade`: the new `0001`'s downgrade drops tables that never existed on prod, so it fails halfway.
  - Wiping `gs://btvaroska/fleetforge/`: it deletes the flasher's agent.
  - Restarting or recreating mosquitto or its volume to clear the clients: ops-log F-2026-09-23-002.
- **Gotchas for the next schema-rewriting release.** Remove the fleetforge containers with `docker compose rm -sf fleetforge-api fleetforge-ingestor`, not stop: `docker rollout` on a stopped service runs `compose up --no-recreate` and can revive the old container on the old image. The scoped deploy recreates the ingestor before the api migrates (R2b-be-5), so restart the ingestor once the api is healthy. Nested `ssh '…'` around `sh -c '…'` eats SQL string literals; feed SQL on stdin.
- **Owner questions carried.**
  - The S0-sec-1 named assumption (a completed password reset marks the Firebase email verified) stays unchecked: Firebase is not configured on prod, and P1 adds no required setting.
  - Whether to configure `FIREBASE_PROJECT_ID`, `FIREBASE_WEB_API_KEY` and `FIREBASE_SUPERADMIN_EMAILS` on prod, which is a prod env change.
- **Procedure:** `docs/runbooks/prod-db-refresh.md` (the prune script was validated on dev MinIO). **Proposals:** `docs/features/projects-and-users.md` → *Owner proposals (S0-rel-1)*, patches A-D for `spec/prd.md`, `CRITICAL.md`, `design/architecture.md` and `design/production.md`.

---

## 2026-10-19 — S0-bug-7: member add is PUT /members/{username} with {"role"}; the dashboard client was wrong; remove/revoke/disable ask first

- **Cause.** `api.ts::upsertProjectMember` sent `POST /v1/projects/{slug}/members` with `{username, role}`. The backend has only `PUT /members/{username}` (and `GET /members`), so FastAPI answered 405 and the drawer could not add anyone.
- **Decision.** Keep the backend contract and fix the client: `PUT /v1/projects/{slug}/members/{username}`, both segments URL-encoded, body exactly `{"role"}` (`ProjectMemberUpsert` is `extra="forbid"`, so a `username` key would 422). No change to `routers/projects.py`. While there, Remove member, Revoke token and Disable user in `ProjectAccess.tsx` ask first with an inline confirm line (the `CommandControls` idiom, one `pending` state, cleared on Keep, completion, drawer close and project switch). Enable stays one click.
- **Why.** PUT is the documented, idempotent contract, symmetric with `DELETE /members/{username}`, and the S0-bug-6 tests already use it.
- **Rejected.** A POST alias (two shapes for one upsert). Switching the backend to POST (churns the documented contract and the test sites). `window.confirm` (no precedent here, jsdom lacks it).
- **Tests.** `tests/test_projects_and_users.py::test_a_member_added_with_put_is_listed_and_post_is_not_a_route` (PUT 200 and listed, second PUT upserts, `username` in body 422, POST 405, unknown user 404). `frontend/src/ProjectAccess.test.tsx`: stateful-mock add test plus confirm tests for remove, revoke, disable and one-click enable. Mutation-checked (POST in `api.ts`, ungated Remove -> tests fail).

---

## 2026-10-19 — S0-sec-2: partition-profile writes are superadmin only, the list is filtered, pinned tokens never mint, and other projects' device ids read as unknown

- **Cause.** `routers/partition_profiles.py` depended only on `AdminDep`, so any viewer or `read`-scoped token could create, adopt or delete fleet-wide profiles, and the list returned `device_ids` (and `detected_device_id`) from every project. In `create_admin_token`, a userless project-pinned `admin` token passed the `owner` check, because `check_project_access` lets an `admin`-scoped token with no user through. It could mint children that survived its own revocation. `revoke_admin_token` compared `row.user_id != admin.user_id`, which is `False` when both are `None`, so a userless pinned token could revoke any userless token, including the operator's unpinned CLI tokens. Device routes loaded the device, answered `404` if missing, then checked access and answered `403`, which told a caller which ids exist in other projects.
- **Decision.**
  - Profile `POST`/`PATCH`/`DELETE` require `admin.is_superadmin and "admin" in scopes` (the `projects.py`/`users.py` idiom), as the first statement, before the fingerprint format check. A non-superadmin gets `403` and never learns whether a fingerprint exists. The RBAC table in `docs/features/projects-and-users.md` ("adopt `partition_profiles`": superadmin only) is the authority. The contradictory sentence in the same doc ("project-owner / superadmin") was corrected.
  - `GET /v1/partition-profiles` stays readable by any read-capable token (`admin` or `READ_SCOPES`). Profiles are listed in full. `device_ids` is filtered in SQL to the caller's projects, and truncated after. `detected_device_id` is shown only when a `devices` row with that id is in a visible project, otherwise `null`. Unrestricted callers get the old output.
  - New `deps.visible_project_ids(session, admin) -> frozenset | None`. It mirrors the visibility half of `check_project_access` (pin, superadmin, memberships, unpinned userless = all), not `auth.visible_projects_for`, which differs for a superadmin user holding a non-`admin` unpinned token.
  - `create_admin_token` refuses any project-pinned caller (`403`). The existing `owner`-for-pinned and superadmin-for-unpinned rules are unchanged.
  - `revoke_admin_token`, for non-superadmins: own = `row.id == admin.token_id` or (`admin.user_id` is set and equals `row.user_id`). A pinned caller may only touch rows pinned to its project. Not-own is unchanged: row must be pinned, caller must be a user who is `owner` there.
  - `check_project_access` now checks visibility (pin, then membership) before scope and role, and takes `not_found: HTTPException | None`. When given, a project the caller cannot see raises it instead of `FORBIDDEN`. Device routes in `commands.py`, `deploys.py`, `devices.py` pass their own `404 "no such device: {id}"`, and `revoke_enrollment_token` passes `UNKNOWN_TOKEN`. **Invariant:** with `not_found=None` the allow/deny outcome is unchanged. The checks are the same conjunction, only reordered, and every denial is still `FORBIDDEN`. `resolve_project_for_request` does not pass it. `READ_SCOPES` is a module constant shared by `check_project_access` and the profile list.
- **Why.** The profile table has no project, so any write by one project's owner changes the deploy gate of every project. Superadmin is the only role that spans projects. Minting from a pinned token was the one way for a credential to outlive its revocation. Sessions are always unpinned, so the dashboard loses nothing.
- **Rejected.** Project-owner profile writes (one project's owner changing every project's deploy gate). Refusing `GET` to non-superadmins (breaks the upload form's layout picker for viewers and operators). Capping userless pinned `admin` tokens below `owner` everywhere: it contradicts the documented "`admin` in scopes grants full access to the pinned project", and members management relies on it. A follow-up if wanted.
- **Known limits (follow-up proposals, not filed).** On `?project=` routes, project-slug existence is still distinguishable (`404` unknown slug vs `403` invisible). The dashboard still shows the profile add/adopt controls to non-superadmins, who see the `403` banner. No existing test asserted `403` for a cross-project device id, so none changed.
- **Tests.** `tests/test_projects_and_users.py`: `test_a_viewer_or_read_token_cannot_write_partition_profiles`, `test_the_profile_list_shows_only_the_callers_projects_devices`, `test_a_project_pinned_token_cannot_mint_tokens`, `test_a_userless_project_token_cannot_revoke_other_userless_tokens`, `test_a_device_in_another_project_is_indistinguishable_from_an_unknown_one`, `test_an_enrollment_token_in_another_project_is_unknown`. Each was mutation-checked (guard removed -> test fails).

---

## 2026-10-19 — S0-sec-1: an unverified Firebase email cannot log in; ID tokens get iss + revocation checks; invites create the Firebase account

- **Cause.** `_resolve_invited_firebase_user` bound a `users` row by `firebase_uid OR email` and nobody read `email_verified`. Firebase email/password sign-up is callable with the public web API key, so anyone could register an invited address that had not logged in yet (or a `FIREBASE_SUPERADMIN_EMAILS` address with no row), log in, take that role up to superadmin, and lock the real person out. `sign_in_with_password` also hard-coded `email_verified=True` when `project_id` was unset. `verify_firebase_token` (google-auth) checks signature, `exp`/`iat` and `aud` but not `iss`, and there was no revocation check. Separately, the invite sent `PASSWORD_RESET` to an address with no Firebase account; Firebase answered `EMAIL_NOT_FOUND`, the API logged a warning and returned 201, and the invitee never got an email.
- **Decision.**
  - An identity whose email is not verified is refused with `403` "this email address has not been verified", as the first statement of `_resolve_invited_firebase_user` (before any DB read, the superadmin bootstrap or uid binding), with `limiter.record_failure`. Resolution is uid first, then email.
  - `FirebaseIdentity.email_verified` defaults to `False`. With a project id it is the token claim AND the live `accounts:lookup` `emailVerified`; without one, `sign_in_with_password` takes it from `accounts:lookup`.
  - `_verify_token_sync` also requires `iss == https://securetoken.google.com/<project>`, a non-empty `sub` of at most 128 chars, and an integer `auth_time` not more than 60 s in the future.
  - `verify_id_token(..., api_key=None, check_revoked=True)`: revocation is done the way `firebase_admin` does it, but with the web API key: `accounts:lookup` with the id token, refused if `disabled` or `auth_time < validSince`. `check_revoked` without an API key raises plain `FirebaseAuthError` (503), never a silent skip. `/v1/auth/firebase` therefore needs both `FIREBASE_PROJECT_ID` and `FIREBASE_WEB_API_KEY`.
  - Invite and `PATCH send_password_reset` go through `routers/users.py::_send_setup_email`: `accounts:signUp` with a random, never-stored password (an existing account, `EMAIL_EXISTS`, is left in place), then `PASSWORD_RESET`. `UserSummary.email_delivery` reports `sent` / `failed` / `not_configured` / `null`; the status stays 201/200 because the row exists. The dashboard shows an error for `failed` and `not_configured`.
  - `AuthContext.is_superadmin` and `MeResponse.is_superadmin` default to `False`. `require_admin` always passes the value, so behaviour is unchanged; only the fail-open default is gone.
- **Why.** Sign-up cannot be disabled in the Firebase project because the server's own invite uses `accounts:signUp` with the same API key (no service account), so the defence has to be "unverified proves nothing". The lookup is the account's current truth; the claim is what was true at mint time, and both are required.
- **Named assumption (owner check at S0-rel-1).** Completing Firebase's password-reset link marks the account's email verified. The invitee flow depends on it. If the first real invite on prod answers "this email address has not been verified" after a completed reset, the fallback is to send `VERIFY_EMAIL` at invite time with the `idToken` that `accounts:signUp` returns when we created the account (`returnSecureToken: True`). Not built now.
- **Residual risk (follow-up proposal, not filed).** A squatter who pre-registers an address can send `VERIFY_EMAIL` with their own id token; if the real owner clicks that link, the squatter's account (with the squatter's password) becomes verified. Not fixable on our side without a service account. Mitigations: require `passwordUpdatedAt` (from `accounts:lookup`) >= the user row's `created_at` before the first uid binding, or move to `firebase_admin` with a service account and disable self sign-up.
- **Rejected.** Disabling self sign-up in Firebase (breaks the invite path without a service account). `firebase_admin` plus a service account (new credential, V2 scope). Trusting the token claim alone for `email_verified` (the lookup is current truth). Also sending `VERIFY_EMAIL` at invite (an attacker-triggered verify link is the thing to avoid encouraging).
- **Tests.** `tests/test_projects_and_users.py`: `test_an_unverified_email_cannot_claim_an_invite`, `test_an_unverified_bootstrap_superadmin_email_is_refused`, `test_an_invite_to_a_new_address_creates_the_account_and_sends_the_email`, `test_an_invite_reports_when_the_email_cannot_be_sent`, `test_a_password_reset_for_a_pre_fix_invitee_creates_the_account`, `test_auth_context_and_me_default_to_not_superadmin`. The fake now raises `EMAIL_NOT_FOUND` for a reset to an address with no account, as Firebase does. `tests/test_firebase_auth.py` (unit, no DB): issuer, `sub`, `auth_time`, revocation, disabled, fail-closed without API key, claim AND lookup, no invented `email_verified`, `create_account`, error mapping. `frontend/src/ProjectAccess.test.tsx` covers the `failed` / `not_configured` messages. Prod does not configure Firebase today, so nothing changes live on deploy.

---

## 2026-10-19 — S0-bug-6: P1's 11 broken tests were wrong, not the code; an arrival belongs to its token's project and an orphan is dropped on purpose

- **Cause.** P1 shipped with its tests never run. 4 failures in `tests/test_projects_and_users.py` and 7 arrival tests in `tests/test_api_devices.py`. The TODO blamed the progress join for all of them; only the 7 arrival cases were.
- **Decision (per case; the backend matches `docs/features/projects-and-users.md` in each, the test changed).**
  - Member add: the backend has only `PUT /v1/projects/{slug}/members/{username}` with `{"role": ...}`; tests no longer POST. **S0-bug-7** owns the shape and `frontend/src/api.ts:759` (which POSTs); if it picks POST it updates these 3 test call sites.
  - Stage payload digest is `publisher.last["artifact"]["sha256"]`.
  - Minting a project-pinned token needs `owner`; the RBAC test now asserts the operator is refused (403), then promotes bob to owner. Setup calls there now assert their status.
  - Firebase fixture id_tokens are 16+ chars (`FirebaseLogin.id_token` keeps `min_length=16`).
  - Arrivals: the fixture wrote random token ids. `add_progress` now inserts a real token; `_wipe_fleet` clears tokens and non-default projects.
- **Why.** A board still arriving has no `devices` row, so the enrollment token it reported under is its only link to a project. `LATEST_PROJECT_SQL`'s inner join drops a row whose token has no row, explicitly (comment + test). Production cannot write one: `POST /v1/device-progress` loads the token first and tokens are revoked, never deleted. `latest_progress` now requires `project_id` and the unscoped `LATEST_SQL` is deleted (it would be a cross-project leak). A comment on `DeviceProgress.token_id` says any future token purge must keep tokens `progress_window_s` past `expires_at`/`revoked_at`.
- **Known limit.** A board enrolled into A and moved to B within `progress_window_s` (900 s) can show in A's arrivals for up to 15 min, since A's list does not hold the device. Cosmetic and bounded; possible follow-up.
- **Tests.** `test_an_arrival_is_listed_only_under_its_tokens_project`, `test_an_arrival_whose_token_row_is_gone_is_dropped`; each fails under its mutation (LEFT JOIN keeping orphans; hard-coded default project).

## 2026-10-19 — S0-bug-5: the verbs mint a fresh command id on every POST; P1's never-defined "reuse the latest unanswered command" is dropped

- **Cause.** P1 (d822f1b) imported `latest_unanswered_command` into `api/routers/commands.py` and added a reuse branch in `_send()`; the function never existed, so the api could not boot and pytest could not collect.
- **Decision.** Drop the import and the branch; `_send()` is the R5-be-2 body again (one new id and one row per POST, record, commit, publish). P1's `check_project_access` calls are untouched. Reaffirms R5-be-2 (2); supersedes nothing.
- **Why.** The firmware keeps the id of the last command it rebooted for in NVS and silently drops a repeat. A reboot's ack can die with the reboot, leaving its row unanswered for ever, so reuse would make every later Reboot/Rollback a no-op for that board. Deploy-intent reuse (`routers/deploys.py`) is a different, bounded mechanism and is unchanged.
- **Test.** `test_a_repeated_verb_is_a_new_command_even_unanswered` (rollback, reboot).

## 2026-10-16 — A failing self-test rolls back and `apply` waits for the safe window, proven end to end: the simulator gains `--safe-window file:PATH` (a window that opens), `just selftest-window-e2e` drives both through the real stack, and `lib-quickstart` phase 2f sends the real `apply` verb into an unsafe window; every QEMU phase checks `deploy.selftest` (R5-test-1)

- **Built** (no spec, firmware, migration, API, ACL or env change). Simulator: `--safe-window file:PATH`, `StageRunner._unsafe_now()` / `release_if_safe()` / `_carry_out_rollback()`, a `_safe_window_loop` worker in `file:` sessions only, `check_safe_window()` (also the CLI's argparse `type`). New `scripts/selftest_window_e2e.py` + `just selftest-window-e2e` (stdlib only, 7 scenarios, `E2E_BREAK`). `scripts/lib_quickstart.py`: `expected_selftest()` asserted on 2b/2c/2d/2e, build W and phase 2f `phase_apply_waits`, `check_window_log()`, `wait_for_lines(poll=)`, the dev-only `--only-safe-window`. Details: `docs/features/self-test.md` -> *R5-test-1 as built*.
- **Decisions.**
  - (1) **The simulator's window is a file, not a timer.** `file:PATH` is safe iff PATH exists, asked where the firmware asks its rule (once per verb after every refusal, and for an `apply: auto` stage), and polled every 250 ms (`SAFE_WINDOW_POLL_S`, twin of `FF_SAFE_WINDOW_POLL_MS`, pinned) only while a `file:` session runs. A harness opens the window by creating the file: both branches of a real predicate, no race against a clock. `auto` and `hold` are byte-identical to before (`hold` still never acts, even through `release_if_safe`). A release mirrors `ff_mqtt.c::command_reboot_task`: an apply clears the stage before its first publish (so a later `cancel` is refused, as once the firmware committed), a cancelled deferred apply is never released, a rollback does what the accepted verb does, a reboot restarts with `reset_reason: sw`.
  - (2) **Phase 2f sends the real verb into a FRESH unsafe stretch.** Build W's `safeNow()` is the runbook's clock (unsafe 60 s of every 80 s) and prints each flip; loop() asks it first so the flips are visible while nothing waits. The verb goes out right after a new `window: unsafe` line (at most one message, ~6 s, late), leaving ~50 s of unsafe time. The verdict reads the sketch's own `window:` lines and wall-clock deltas, never QEMU tick timestamps (5-8 s ahead of `millis()`).
  - (3) **The kill race is the harness's, and named.** After `state=rebooting` the agent drains 1.5 s and calls `esp_restart()`, whose soft reset panics in QEMU with B PENDING_VERIFY (the bootloader would abort B). The script waits ≤ 0.4 s for the statuses to land and `docker kill`s the container (not `just`), then boots it; the boot pointer was written at `staged`, so B boots as the apply would have. A `rebooting for command` followed by `rst:` fails the run as a harness race, not an agent defect (rerun).
  - (4) **`deploy.selftest` is checked on the real agent's details**: B passed, R `null` (legacy wording), F failed with the 27-byte reason `i2c sensor 0x48 did not ans`, T timed_out, each `returned_to` B's slot; and in 2f.
- **Named gaps.** The simulator still does not gate the confirm-timer / self-test rollback on the window (D12) and does not read the stage's `confirm_timeout_s` (both "not modelled"; one follow-up proposed, not filed). QEMU cannot show the soft reset itself (the kill stands in). A deferred rollback / reboot released by the window is proven in the simulator's unit tests and R5-fw-5's hand T2 only, not in a harness.
- **T2.** `just selftest-window-e2e` 7/7; each `E2E_BREAK=<id>` exits 1 with exactly that FAIL, 6/7; `just commands-e2e` 9/9, `just update-e2e` 7/7; `just lib-quickstart --skip-idf --only-safe-window` PASS (dev, 1385 s) and the full `just lib-quickstart` `== PASS (2612 s)` on the first try (2b passed, 2c null, 2d failed `i2c sensor 0x48 did not ans` ota_1, 2e timed_out ota_1; 2f `deferred`, 49 samples at `awaiting_safe_window`, `window: safe at millis 140064` before `state=applying` 58 s after the verb, B confirmed with the ordered steps, result still `deferred`); the kill race was never lost, so no rerun was needed.

## 2026-10-16 — Dashboard command seam: Apply, Cancel update and Reboot (no Rollback button), gated by announced capabilities; a "Hold at staged" box; the self-test row from `deploy.selftest` only; `cancelled` is not a failure; a rollback wait is named as one (R5-fe-1)

- **Built** (frontend only; no spec, Python, migration, simulator, firmware, ACL or env change). `api.ts` mirrors `DeploySelfTest`, `CommandSummary`, `CommandAccepted` and the OPTIONAL `DeploySummary.selftest` / `DeviceSummary.last_command`, plus `applyDeploy` / `cancelDeploy` / `rebootDevice`. New `commands.ts` + `CommandControls.tsx`; `deploy.ts` (`safeWindowWait`, `deployStateLabel`, `isCommandRollback`, `cancelled` label); `deployTimeline.ts` / `DeployTimeline.tsx` (`ended` status); `deployResult.ts` (`cancelled` outcome, self-test reasons and row, command rollback); `PrecheckCard` hold box; `DeployCell` wiring. Harness `frontend/scripts/commands-e2e.mjs`, `just commands-e2e`. Details: `docs/features/self-test.md` -> *R5-fe-1 as built*.
- **Decisions.**
  - (1) **Controls: Apply, Cancel update, Reboot.** Cancel is in: it sits on the same staged transaction as Apply, is the only way out of a held stage from the dashboard and makes `cancelled` reachable without curl. **Rollback is out** (not in the task title, the destructive verb; the REST route stays for scripts; its answer still renders in the last-command line). No `api.rollbackDevice`.
  - (2) **"Hold at staged: install only when I click Apply"** in the pre-check card, only for a board announcing `apply`, default off, reset with the card. The pre-check is still asked with `apply: auto` (the only server difference is the capability gate, which the box already enforces); only Send carries `on_command`.
  - (3) **Offer rules** (`commands.ts::offeredCommands`) read the board's announced `capabilities` and the server's read model, never re-derive server state: Apply = `apply` capability, non-terminal, non-empty `cmd_id`, state `staged`; Cancel = `cancel` capability, non-terminal, `cmd_id`, state in {staging, downloading, verifying, staged} or an APPLY wait (never `requested`, nothing from `applying` on, never a rollback wait); Reboot = `reboot` capability, always (the board's refusal is shown). A legacy `['ota']` board gets no controls and no hold box.
  - (4) **The last-command line** `<verb> · <answer>[: detail] (<ago>)`: `result: null` = "sent, no answer yet" (plus "the board is offline; it answers when it next connects" when `online` is false), never a timeout, spinner or "stuck"; `accepted` / `deferred: the board waits for a safe moment` / `refused` (class `bad`) / any other word as itself; verbs and answers through `Object.hasOwn` lookups with a raw fallback.
  - (5) **Reboot has a confirm step** (`Reboot <name> now? It restarts on <fw>.`); Apply and Cancel post on one click. After a 202 the cell re-reads (`onDeployed`); `device_online: false` shows a local "queued" line; a 401 bounces to login; any other error is the server sentence verbatim in `role="alert"` (`DeployCell.fail()` precedent). Buttons are disabled while a POST is in flight and named per board (`aria-label`); `Cancel update`, not `Cancel`, because the pre-check card's `Cancel` shares the cell.
  - (6) **`safeWindowWait`**: the two exact detail strings decide; otherwise a wait after a `rebooting`/`confirming` step is a rollback, else an apply (the simulator's auto hold has no detail). Kept on raw state names inside `deploy.ts`, so no circular import with `deployTimeline.ts`. A rollback wait is labelled "rolling back at the board’s next safe moment — it has decided, and may wait indefinitely" and sits on no timeline milestone; neither wait gets a deadline or a stall.
  - (7) **`cancelled`**: label "cancelled — the board keeps its current image", not a bad state, its own result outcome with no tone, no next action and no Send again, and a muted `ended` (■) timeline end item instead of `failed`.
  - (8) **Self-test** comes ONLY from the server's `deploy.selftest` (R5-be-1): new reasons for passed / failed / timed out, next actions that never say "send it again" (the `resend` invariant holds), and a `Self-test` row after `Board says`. An older API (no `selftest`) gets no row even when `detail` carries the grammar.
  - (9) **Command rollback** (`detail` head `rolled back by command`): verdict note "by an operator command", reason "A rollback command returned the board to …", no next action. Its own transaction has NULL versions and no `sent_by`; no string prints `null`.
- **Rejected.** A Rollback button (follow-up proposed: "Dashboard Rollback control with a confirm step, for a confirmed image"); a client regex for the self-test grammar; a backend `apply` field on `DeploySummary` (the server's 409 "was sent with apply: auto" covers the race); a timeout on a deferred or unanswered command; an `aria-live` region in the cell.
- **Tests.** Frontend 741 -> 828 (existing expectations unchanged). Mutation checks, each red then reverted: Apply offered without the capability (4 red); the rollback detail classified as an apply wait (3 red); the Self-test row parsed from `detail` (3 red); Reboot posting without the confirm (2 red).
- **Evidence.** T1: `npm run typecheck && npm test && npm run build` green, no warnings. T2: `just commands-e2e` 9/9 pass, exit 0 (boards V/H/L, run `mvbkiucs`; apply cmd_id `0d3a38b1a1574e9ba40a2b3eb44264a5`, Apply command `cf0e435a4ae5464491a1f9f3e470fc3b`, deferred Apply on H `05fcf9a549db44c48309178a5182f5ac`, refused Reboot on H `892e8eb08fd241efab1fc555899ce9a7`). Breaks: each `E2E_BREAK=<id>` run exits 1 with exactly that scenario FAIL and 8/9 pass (all nine ids). Regressions: `just update-e2e` 7/7, `just health-e2e` 4/4. Screens: `/tmp/ff-r5-fe-1/{selftest-failed,apply,apply-waits,reboot}.png`.
- **Proposed spec addendum** (owner, optional; `spec/` is not edited): none required. The TODO line says "the self-test outcome from the detail grammar"; as built the dashboard reads the server's parse (R5-be-1), which is the same grammar parsed once.

---

## 2026-10-16 — Sleepy helpers: a code-set power class overrides ff_cfg and is announced (frozen at session start, strict validation); isConnected() is per connection (announce acknowledged on it); isReadyForSleep() is D14 plus "this boot checked in" and "telemetry drained", read without NVS and never through ff_ota_txn_phase() (R5-fw-7)

- **Built** (unreleased, no version bump). Maker header `include/ff_sleep.h` (`ff_power_class_t`, `ff_set_power_class()`, `ff_is_connected()`, `ff_is_ready_for_sleep()`), `src/ff_sleep.c` (TAG `ff-sleep`), private `src/ff_sleep_internal.h` (`ff_sleep_freeze()`, `ff_sleep_power_override()`). `ff_mqtt.c`: file statics `s_acked_this_connection` / `s_checked_in`, cleared in `on_connected()` before the announce and at disconnect, set by one `if` before the unchanged confirm block in `MQTT_EVENT_PUBLISHED`; `ff_sleep_freeze(cfg)` after the safe-window freeze, before `ff_txn_init()`; read-only `ff_mqtt_is_connected()` / `ff_mqtt_sleep_blocker()`. `ff_ota.c`: read-only `ff_ota_in_flight()`. `ff_telemetry.c`: read-only `ff_telemetry_drained()`. `announce_object()` takes the override. Arduino `setPowerClass()` / `isConnected()` / `isReadyForSleep()` (`enum PowerClass`), `ff-sleep` in `FF_LOG_TAGS`; `basic_idf/main/main.c` declares `FF_POWER_ALWAYS_ON` and prints both helpers (70 lines). No spec, server, simulator, capability, announce key or wire change; no line of the confirm timer, the rollback gate, the verbs, the self-test hold or the cancel path changed. Details: `docs/features/self-test.md` -> *R5-fw-7 as built*; runbook `docs/runbooks/agent-qemu.md` -> *Sleepy helpers (R5-fw-7)*.
- **Decisions.**
  - (1) **The override is accepted** (R5-spec-1's flag decided): `ff_cfg` is never rewritten by an update, so a firmware that becomes a sleeper would otherwise announce `always_on` for ever and be shown offline after its first sleep; the server already updates `power_class` on every announce. The session logs once which class it announces and which `ff_cfg` value it replaces.
  - (2) **Frozen at session start**, declared right after `begin()`/`fleetforge_start()` (never before: `begin()` FIRST); the enroll body keeps `ff_cfg`'s class and the first announce corrects it.
  - (3) **Strict validation**: sleepy `1..INT32_MAX` s, always_on 0, else `ESP_ERR_INVALID_ARG`; second or late `ESP_ERR_INVALID_STATE`; one log per refusal.
  - (4) **`isConnected()`** = session up AND its announce acknowledged on this connection (false under `FF_ROLLBACK_TEST`).
  - (5) **`isReadyForSleep()`**, first hit wins: not checked in since boot; rollback decided or waiting; command reboot pending; transaction not closed; outbox not empty; update in flight; boot pointer on another slot; running image not confirmed (VALID/UNDEFINED/no otadata entry are confirmed, any other error is not); telemetry not drained. "Checked in" and "telemetry drained" are deliberate additions to the spec list (a loop that sleeps on `isReadyForSleep()` must not sleep before the first connect; a RAM sample is lost at a sleep).
  - (6) **No NVS, never `ff_ota_txn_phase()`** on the readiness path (its staged branch may erase a torn record); never logs; never from an ISR or esp_timer; may wait for esp-mqtt's lock during a reconnect (documented). Telemetry readiness never wakes the flush task (it would defeat the 5 s payload bound), so it lags `metric()` by up to 5 s (documented).
- **Named gaps.** After a staged `cancel` the running entry is NEW (R5-fw-4 gap (a)), so a sleepy board stays awake until a reset or power cycle; **proposed follow-up**: "Re-mark the running image valid after a boot-slot put-back (closes R5-fw-4 gap (a); sleepy boards stay awake after a cancel)". An `on_command` stage keeps the board awake until `apply`/`cancel`. A wake that never reaches the broker never becomes ready (the firmware's own wake limit must exceed the confirm deadline). The agent's first heartbeat is `hb_s` after the connect, so a short wake sends none. No real deep sleep in QEMU.
- **Tests.** New `tests/test_sleep.py` (36); `test_ota_component.py` (`MAKER_HEADERS`, `INTERNAL_HEADERS`), `test_telemetry.py` (the internal header's third hook), `APP_SIZE_BUDGET_BYTES` raised to the measured bytes (+0.9-1.0 KB). Mutation checks, each red then reverted: boot-pointer check dropped; outbox check dropped; `s_checked_in` set at connect; freeze after `esp_mqtt_client_start(`; override before the freeze; `ff_ota_txn_phase()` in the readiness path.
- **Evidence.** T1: `just test` 2370 passed; four `BUNDLE OK`, 0 warnings; `lib-quickstart --build-only` and `lib-arduino-check` `== PASS` (`T verifyRollbackLater` in both ELFs); `agent-qemu-smoke esp32` `HARNESS OK`. T2 (QEMU, dev stack 8088): S `--fresh` logs the `ff-sleep` override line before `connecting to`, 6 × `connected=0 ready=0` before the ack, `connected=1 ready=1` 1.0 s after it, wire and `GET /v1/devices` `sleepy`/120; an `on_command` stage of S2 reads `ready=0` from `staging` through `staged` (67 s), the deferred `apply` and its wait; S2's boot `ready=0` until the `confirmed` PUBACK, `ready=1` 0.11 s later; F (failing self-test) `ready=0` for its whole life, S2 `ready=0` until its `rolled_back` is acknowledged; a deferred `reboot` `ready=0` until it happens; the stock agent logs `no power class declared` and announces `always_on`/null.
  Full `just lib-quickstart` `== PASS (2349 s)` (phases 2a-2e as in R5-fw-6, capabilities unchanged, credentials in 0 transcripts).
- **Proposed spec addendum** (owner, optional; `spec/` is not edited): none is required (the spec never says where `power_class` comes from, and D14 is Patch A4). If the owner wants the readiness list in the spec when Patch A4 (D14) is applied, append: "A firmware helper that reports readiness for sleep also reports not ready until the device has checked in on this wake, and while recorded telemetry is unpublished; a power class declared by the firmware replaces the provisioned one in the announce."

---

## 2026-10-16 — Rapid-crash safe mode: three panic/watchdog resets within 60 s on the same confirmed image hold the firmware's own code behind the agent; the state is one RTC_NOINIT record (never NVS), the lifetime breadcrumb a FreeRTOS task (never esp_timer); every non-brownout, non-counted reset leaves; `safe_mode: true` in the announce only (R5-fw-6)

- **Built** (unreleased, no version bump). Public non-maker `include/ff_safe_mode.h` (`bool ff_safe_mode_check(void)`), `src/ff_safe_mode.c` (TAG `ff-safemode`), private `src/ff_safe_mode_internal.h` (`ff_safe_mode_active()`). All three boot paths call the check right after `ff_mqtt_arm_confirm_timer()`; `Fleetforge.cpp` `begin()` and `basic_idf/main/fleetforge_start.c` then hold the calling task after `s_started = true` (`hold_in_safe_mode()`, noreturn, a W line every 5 min); `agent_main.c` never holds. `ff_identity.c`: `ff_identity_reset_reason_name()` wrapper, `ff-id: safe_mode true (announced)` before the `device_id` line, `"safe_mode": true` between `reset_reason` and `lib_marker`. Details: `docs/features/self-test.md` -> *R5-fw-6 as built*; runbook `docs/runbooks/agent-qemu.md` -> *Rapid-crash safe mode (R5-fw-6)*.
- **Decisions.**
  - (1) **RTC_NOINIT, never NVS**: survives panic/watchdog/software resets, lost at power-on (= the spec's power-cycle exit), no flash wear, available before NVS; magic + `esp_rom_crc32_le`, re-sealed after every write; invalid = fresh (fail-safe).
  - (2) **Window** = the two lifetimes between the first and third counted reset sum to <= 60 s (`FF_SAFE_MODE_CRASHES 3`, `FF_SAFE_MODE_WINDOW_S 60`, `_Static_assert`ed).
  - (3) **Breadcrumb = a self-deleting FreeRTOS task** (`ff_alive`, 2 KB, prio 5), only on a boot that began with a counted crash and is not latched; writes an upper bound each second up to 60; no log/NVS/lock. **Never an esp_timer**: an alarm pending across a reset is the likely trigger of QEMU's `esp_timer_impl_init` loop.
  - (4) **Ladder**: fresh on invalid / `poweron` / another slot / not confirmed (only VALID, UNDEFINED or no otadata entry are confirmed); `panic`/`int_wdt`/`task_wdt`/`wdt` count; `brownout` clears the count and keeps the latch; everything else (incl. `sw`, the `reboot` verb's exit, and `cpu_lockup`) is fresh. No line of `ff_mqtt.c`/`ff_ota.c` changes.
  - (5) **Non-maker public header** (all three paths call it, so no surface rule changes); the flag's read is private.
  - (6) **The hold sits after the agent task starts**, so the board stays online; the sketch's self-test/safe-window are never registered, so it announces neither and reboots at once when told.
  - (7) **A failed update returns to the image that crashed** and re-enters safe mode three crashes later: accepted.
- **Deviations from the plan.** First counted crash logs `after 60 s or more (or unmeasured)`; `safe mode left: reset by poweron` when a valid latched record meets POWERON (ESP32 EN button); T2 step 5 proved the `sw` exit with an extra variant (the `reboot` verb's next boot always hits the emulator loop, retried once); T2 step 7 used on_command + apply + power cycle instead of `auto`; the implementer did not flip TODO.md (CRITICAL review commits).
- **Tests.** New `tests/test_safe_mode.py` (33); `test_ota_component.py::INTERNAL_HEADERS` + `safe_mode`; `APP_SIZE_BUDGET_BYTES` raised to the measured bytes (+1.6-1.8 KB). Mutation checks, each red then reverted: brownout counted; slot compare dropped; hold removed from `begin()`; `safe_mode` emitted false; an `esp_timer_` call in `ff_safe_mode.c`.
- **Evidence.** QEMU: X35 + bad cfg 7 boots `crash reset 1..6 of 3`, zero `SAFE MODE`; X25 + bad cfg `SAFE MODE` on boot 4, no `fw6x:` for 185 s, hold line; X + good cfg: `SAFE MODE`, wire `"reset_reason":"panic","safe_mode":true,"lib_marker":1`, capabilities exactly `["ota","apply","cancel","rollback","reboot"]`, heartbeats, no `fw6x:`/`rst:` for 225 s; power-cycle and `sw` exits; update exit (B confirms, no `safe_mode`); an unconfirmed X's crash not counted (B `rolled_back`, no `ff-safemode` line); full `lib-quickstart` `== PASS`, stock agent announce without `safe_mode`.
- **Named gaps.** Crashes before `begin()`/`fleetforge_start()` are invisible; lifetimes are measured from the check (window a little over 60 s); a crash mid-write or an RTC-domain watchdog reset wipes the record (no safe mode); a hog at prio >= 5 under-reports a lifetime; a PENDING_VERIFY image that crashes before its own check is charged one crash to the image the bootloader returns to; the stock agent can be in safe mode with nothing to hold.
- **Proposed spec addendum** (owner; Patch A6, append to the `safe_mode` paragraph): "The three resets count only while the same confirmed image runs; a USB-flashed image counts as confirmed. "Within 60 s" is measured over the lifetimes of the boots between the first and the third reset. A brownout neither counts nor leaves safe mode; any other reset (a software restart, the reset pin, USB, JTAG, a deep-sleep wake) leaves it. An update that fails returns to the image that crashed, which re-enters safe mode after three more crashes. A device that runs only the agent reports `safe_mode` too." Open question: should `cpu_lockup` count?

---

## 2026-10-16 — The four verbs over REST: apply/cancel/rollback/reboot are recorded in a new device_commands table, committed before the publish, never a deploy_events row; the board's first up/ack answer wins; cancelled is terminal and out of the KPIs; a rollback's own transaction counts nowhere; on_command needs the apply capability; the simulator learns the verbs (R5-be-2)

- **Built.** `api/routers/commands.py` (new): `POST /v1/devices/{id}/deploys/{cmd_id}/apply|cancel`, `POST /v1/devices/{id}/rollback|reboot` (admin-only, no body, 202 `CommandAccepted`), `GET /v1/devices/{id}/commands/{command_id}` (`CommandRecord`). `device_commands.py` (new): the single writer and reader of `device_commands` (`record_sent`, `mark_publish_failed`, `record_answer`, `get_command`, `latest_commands`). Migration `0010_device_commands.py` + `DeviceCommand`, `CommandType`, `AckResult`, `DeployState.CANCELLED` in `TERMINAL_DEPLOY_STATES` (`db/models.py`). `broker/commands.py`: verb constants, `VERB_COMMANDS`, `verb_payload()`; `publisher.py` untouched. `deploys.py`: `KPI_EXCLUDED_TERMINAL_STATES`, `counts_in_deploy_kpis()`, `deploy_transaction()`. `deploy_precheck.py`: `apply_refusals()`, `NO_APPLY_CAPABILITY`. Ingestor: `UpChannel.ACK`, `AckPayload`, `ACK_MAX_BYTES`, `_store_ack`; `EventType.DEVICE_COMMAND`, `DeviceEvent.command_id`. `DeviceSummary.last_command` (`CommandSummary`) before `deploy`. Simulator: the verbs, `up/ack`, `_release`, `returnable`, `--capabilities` takes several values. No spec, firmware, frontend, ACL/dynsec, Traefik or env change. Details: `docs/features/self-test.md` -> *R5-be-2 as built*.
- **Decisions.**
  - (1) **Gates, in order, only what the server alone knows:** device missing/decommissioned 404 (the deploy sentence); the verb not in `capabilities` 409 (D9); apply/cancel: `cmd_id` not `^[A-Za-z0-9_-]{1,63}$` 400, no `requested` row for `(device, cmd_id)` 404, a terminal row 409 naming its state; apply: `detail.apply != on_command` 409. No "is it staged" / "is an update in flight" gate: the board answers that on `up/ack` and the server's view can lag.
  - (2) **Record, commit, publish**; a `CommandPublishError` sets `publish_failed`, commits, 503. **No verb writes `deploy_events`**: `record_publish_failure` stays the only server-authored terminal state and its tripwire is unchanged.
  - (3) **`device_commands`**: `id` (the command's own, uuid4 hex), `device_id` FK `ON DELETE CASCADE` (operational, not KPI history), `type`, `cmd_id`, `sent_at`, `sent_by` (R2b-be-4 snapshot), `publish_failed`, and the first answer (`answered_at`, `result`, `answer_cmd_id`, `answer_detail`). No CHECK, no PG enum, no retention sweep.
  - (4) **First answer wins**: one `UPDATE … WHERE id AND device_id AND type AND answered_at IS NULL AND NOT publish_failed RETURNING id`. A board never answers another's command; an unknown id creates no row (INFO: device, `accepted|deferred|refused|<other>`, the id's length); a type mismatch is a WARNING; a duplicate DEBUG. `result` open (printable, 1-16 chars, else dropped); `answer_cmd_id` NULL if longer than 64; `answer_detail` sanitised; detail text never logged. A recorded answer upgrades the frame to `device.command` with `command_id`; anything else stays `device.seen`. Retained acks are dropped before the device lookup.
  - (5) **`cancelled` is terminal** (R5-spec-1 D4/A3): a cancelled `cmd_id` is never reused, closing R5-fw-4 gap (c); the dashboard gets `is_terminal: true` for free.
  - (6) **KPI membership is one pure rule**, `counts_in_deploy_kpis(terminal_state, has_requested_row)`: only transactions with a `requested` row, and not `cancelled`. So a rollback of a confirmed image (its own transaction under the command's id, D5, recorded with NULL versions) is in neither KPI, closing gap (d); a rollback sent during `confirming` ends the stage's transaction and stays in the KPIs as a `rolled_back` (a save for fleet safety, a miss for delivery). R6 implements it; nobody re-derives it.
  - (7) **`apply: "on_command"` needs the `apply` capability**: `apply_refusals()` appended to `refusals()` on `/deploy` (409, before the reuse lookup, mint and INSERT) and `/deploy/precheck` (`deployable: false`). A separate function so `refusals()` and its ~40 test call sites stay as they are.
  - (8) **Read model**: `last_command` = the newest non-publish-failed command per board (`DISTINCT ON`, bounded by the listed ids), `sent_by` without the token id; `result: null` = no answer yet, possibly never.
  - (9) **Simulator**: a verb runs only if announced; a board announcing none is a pre-R5 agent (no ack); one announcing any answers other id'd types `refused`/`not implemented`. Acks QoS 1, retain False, A5 key order. `_release` is the one walk for `auto` and `apply`; `returnable` is set at a confirm and cleared by any rollback; `--safe-window hold` answers apply/rollback/reboot `deferred` (with `awaiting_safe_window` for apply/rollback) and never acts. An `auto` stage parked by `hold` is now held as `staged`, so it is cancellable and a second stage is refused, as on metal.
- **Deviations from the plan.** The routes are in a new `api/routers/commands.py`, not `deploys.py`/`devices.py`: both carry source tripwires scoped to the deploy path, and the verb seam is its own module. `DeployTransaction.ended_state` (the first terminal state, which the 409 sentence names) with `ended` a property, built from SQL so `deploys.py` spells no new `is_terminal=`. The ack type check is part of the `UPDATE`. `--capabilities` became `action="extend", nargs="+"` so `--capabilities ota apply …` works as the acceptance criteria spell it (repeated flags still do).
- **Tests changed on purpose.** `test_api_deploy.py::test_apply_on_command_is_passed_through` (the board now announces `apply`); the no-timer tripwire parametrisation gains `api/routers/commands.py` and `device_commands.py`; `test_events_stream.py::test_the_envelope_keys_are_pinned` (+`command_id`); `test_api_devices.py` key sets/order (+`last_command` before `deploy`); `test_schema.py::EXPECTED_TABLES` (+`device_commands`).
- **Mutation checks** (each red, then reverted): (i) `AND answered_at IS NULL` dropped → `test_the_first_answer_wins`; (ii) the `device_id` match dropped → `test_a_board_cannot_answer_another_boards_command`; (iii) `CANCELLED` out of `TERMINAL_DEPLOY_STATES` → `test_a_cancelled_transaction_is_not_reused`, `test_cancelled_is_recorded_as_terminal`, the `cancelled` 409 cases; (iv) the capability gate skipped → 5 failures in `TestTheCapabilityGate`; (v) a `requested` row written by the rollback route → the zero-deploy-row assertion; (vi) `apply_refusals` not concatenated in `/deploy` → `test_on_command_to_a_board_without_apply_is_409_and_sends_nothing`.
- **T2 evidence** (dev stack 8088; `docker compose restart api ingestor`: `Running upgrade 0009 -> 0010`, `\d device_commands` as designed; throwaway driver `/tmp/r5be2/`, esp32s3 images from `esp32s3.app.head.bin` + the library marker, labels A/B/C/D `1.{0,1,2,3}.0-r5be2-1792165060`; boards `r5be2-1792165060-v` `d600824f2d32` (R5 verbs), `-old` `1a0f62ab2ce3` (`ota`), `-h` `86cc40996a36` (R5 verbs, hold); SSE and `just mqtt-sub 'ff/v1/d/+/up/#'` captured throughout). (1) Legacy: reboot 409 `does not implement reboot (it announces: ota)`, on_command deploy 409 `cannot be told to apply`, pre-check `["no_apply_capability"]` / `deployable: false`, no `command` line in its transcript. (2) B on_command → `S1` `5702b505…` staged; apply → 202 `X1` `a80d0147…`; wire `{"id":"a80d0147…","type":"apply","result":"accepted","cmd_id":"5702b505…","detail":null}`; recorded 0.6 s after the POST; `confirmed` on B 2.6 s after it, steps `requested … staged, applying, rebooting, confirming, confirmed`; `last_command` X1/`accepted`; exactly one `device.command` frame with `"command_id":"a80d0147…"`. (3) Apply on S1 again → 409 `already ended (confirmed)`, nothing published. (4) C → `S2` staged, cancel → ack `accepted`, `cancelled`, `is_terminal: true`; re-deploy C → new `S3` `b09e9cb5…`, `reused: false`; S3 cancelled too. (5) Rollback → 202 `X4` `edb83b80…`; ack `cmd_id` = `edb83b80…`; `rolling_back`, `rolled_back` `rolled back by command; returned to ota_0` under X4; `fw_version` back on A; `deploy.sent_by`/`selftest` null; 0 `requested` rows, `artifact_version` NULL; `counts_in_deploy_kpis(...)` False; a second rollback → `refused` `no confirmed image to return to`, no status, no reboot. (6) Reboot → ack `accepted`/null, next announce `reset_reason: sw`, no `up/status` between; hold board: D on_command staged, reboot → `refused` `an update is in progress` with `cmd_id` = the stage's `3ef7a6d2…`, apply → `deferred` + `awaiting_safe_window` `apply waits for a safe window`, unchanged after 65 s, cancel → `accepted`, `cancelled`. (7) A hand-published `{"id":"a80d0147…","result":"refused","detail":"late"}` as the board → X1 still `accepted`; `bogus-1792165060` → 0 rows, ingestor INFO `answered (accepted) a command id this server did not send it (16 chars); no row created`. (8) `deploy_events` under all nine command ids: exactly X4's `rolling_back` and `rolled_back`. (9) SSE: 273 lines, 9 `device.command` frames, 0 `ffa_`/`ffe_`/signed URLs; `late` in api+ingestor logs: 0. T1: `just test` 2301 passed (ruff, format, mypy clean).
- **Release notes.** Migration 0010 (additive; downgrade drops operational command records only). The api must deploy before or with the ingestor: the ingestor's ack write is in a SAVEPOINT, so an un-migrated schema only logs a WARNING and keeps liveness. R5-fe-1 mirrors `CommandSummary` / `last_command` into `frontend/src/api.ts` as an OPTIONAL field and adds the controls; frames of type `device.command` already trigger the dashboard's re-read.
- **Proposed spec addendum** (owner; Patch A5, after "It keeps the first answer for an `id`…"): "A server that sends these commands keeps the first answer for each `id`; an answer for an `id` it did not send is ignored." Optional: Patch A2, last bullet: "A server also refuses `apply: "on_command"` to a device that does not list `apply`."
- **Gotchas.** `tests/test_api_deploy.py::test_only_publish_failed_authors_a_terminal_state` regex-scans `deploys.py` for `is_terminal=`; the new transaction read uses SQL aliases. The simulator imports nothing from the server; its verb vocabulary is retyped and pinned by `test_the_retyped_verb_vocabulary_is_the_servers`. `just sim` echoes its recipe line, so a `--token` passed on argv lands in a captured transcript (the T2 logs were scrubbed; the tokens were single-use and burned).

---

## 2026-10-16 — Deploys carry the self-test outcome: the server parses the D11 grammar on read (never stored), only on its own state, onto DeploySummary.selftest and the terminal device.deploy frame; KPIs unchanged; the simulator learns a self-test (R5-be-1)

- **Built.** `src/fleetforge/selftest_outcome.py` (pure: `SELFTEST_DETAIL_RE` used with `.fullmatch()`, `SelfTestOutcome(outcome, reason, returned_to)`, `parse_selftest_detail()` for the grammar alone, `selftest_outcome(state, detail)` for grammar plus the state rule). `deploys.py`: `DeploySnapshot.selftest`, set in `latest_deploys` from the newest row only. `api/schemas.py` + `api/routers/devices.py`: `DeploySelfTest` and `DeploySummary.selftest` (defaulted, after `sent_by`; `DeviceSummary.deploy` stays last). `events.py` + `ingestor/handlers.py`: `DeviceEvent.selftest` (reuses the dataclass; terminal row only, same guard as `detail`; `v` stays 1). Simulator (`device.py`, `__main__.py`): the `selftest` capability registers a self-test, `--selftest-fail-marker`, OTA slot labels, `rolled_back_head`, one `_rollback()` shared by the confirm timer and a failed self-test. No migration, no `deploy_events` write change, no spec/firmware/frontend change.
- **Decisions.**
  - (1) **One reader.** The server is the single parser of the grammar (as `is_terminal` is the single copy of the terminal set); no client keeps the regex.
  - (2) **Exposed on read, never stored.** No column, no JSONB key, no migration (`alembic/versions/` is CRITICAL); rows already in the DB parse with no backfill; `record_observed_status`, `_observed_detail` and `sanitise_device_text` are untouched. The stored (sanitised) detail is parsed, never the wire's.
  - (3) **Two surfaces:** `GET /v1/devices` (and PATCH) `deploy.selftest`, and the terminal `device.deploy` frame's `selftest`. Null means "not a self-test outcome" (no self-test registered, a pre-R5 agent, a never-armed image's `confirmed`, a command rollback, the legacy timeout wording, a safe-window wait).
  - (4) **State-consistent.** `passed` only on `confirmed` with no `; returned to` tail; `failed` / `timed_out` only on `rolled_back` (tail optional). Anything else is null. This follows `ff_mqtt.c`, and "passed" beside a rollback would be a lie.
  - (5) **KPIs and the terminal vocabulary do not change.** A self-test rollback is a `rolled_back`: a save for fleet safety, a miss for delivery success. `TERMINAL_DEPLOY_STATES` / `is_terminal` are untouched. R6 must not invent a `selftest_failed` state. `cancelled` comes with R5-be-2.
  - (6) **The simulator learns the self-test** so the server path runs without QEMU and R5-test-1 has a simulated failure to drive. Registered = `"selftest" in capabilities`; a fail-marked image rolls back at once (`rolling_back` best-effort, reboot) and the next session reports `selftest failed: simulated self-test failure; returned to <slot>` (63 B); a registered board that never confirms reports `selftest timed out; returned to <slot>`; broken beats fail-marked; a board with no self-test keeps the legacy wording byte for byte. It does not gate the rollback on a safe window (D12) and does not read `confirm_timeout_s` (R5-be-2 / R5-test-1).
  - (7) **Deviation from the plan.** The plan's third mutation (selftest on non-terminal frames) cannot go red on its own: the only states the grammar accepts (`confirmed`, `rolled_back`) are both terminal, so the frame guard is defence in depth behind the state rule. It is only visible when the state rule is dropped too (done below).
- **Rejected.** Storing it at write time or adding a JSONB key; a `selftest_failed` deploy state; parsing on the client; a separate `up/status.selftest` key (R5-spec-1 already rejected it); trusting a grammar detail on any state.
- **Release notes.** R5-fe-1 must mirror `DeploySelfTest` into `frontend/src/api.ts` as an OPTIONAL field. No migration. api and ingestor can ship in either order: the API field is defaulted and the frame key is additive.
- **Mutation checks** (each red, then reverted): the state rule dropped (18 failures across `test_selftest_outcome` and `test_api_devices`); `.match` instead of `.fullmatch` (5 failures: trailing space and newline, empty slot, `ota 1`); selftest on non-terminal frames alone (still green, see decision 7); the frame guard and the state rule both dropped (`test_a_terminal_row_carries_the_selftest_outcome` red).
- **T2 evidence** (dev stack 8088, ingestor restarted, api hot-reloaded; throwaway driver, labels `5.0.0-r5be1-1792163058`, `5.1.0-r5be1-1792163058-stfail`, `5.2.0-r5be1-1792163058-hang`; board `r5be1-1792163058-01` with `--capabilities ota --capabilities selftest --selftest-fail-marker=-stfail --broken-marker=-hang --confirm-timeout 5`, control `r5be1c-1792163058-01` with `ota` only). Good: `confirmed` in 2.5 s, detail `selftest passed`, `selftest {"outcome":"passed","reason":null,"returned_to":null}`. stfail (`bf0cf228`): `rolled_back` in 4.3 s, detail `selftest failed: simulated self-test failure; returned to ota_1` (63 B), `selftest {"outcome":"failed","reason":"simulated self-test failure","returned_to":"ota_1"}`, steps `... rebooting, confirming, rolling_back, rolled_back`, no `confirmed`, `fw_version` back on the good label. hang (`831e4632`): `rolled_back` in 9.6 s, detail `selftest timed out; returned to ota_1`, `selftest {"outcome":"timed_out","reason":null,"returned_to":"ota_1"}`. Control: `confirmed`, detail null, selftest null. SSE: 64 frames, 34 `device.deploy`; exactly one terminal frame per transaction, its `selftest` equal to the API's; every frame has the `selftest` key, null on every non-terminal and non-deploy frame; no `http://`/`https://` on the stream; the board logs held 0 `ffa_`/`ffe_` tokens. T1: `just test` 2174 passed (ruff, format, mypy src/ scripts/ clean).
- **Gotchas.** The URL redaction (`https?://\S+`) runs before the parse and eats to the next whitespace, so a URL placed directly before `; returned to` in a reason swallows the tail and the whole detail parses to null (a 27-byte reason makes this very unlikely; null is the safe answer). `--selftest-fail-marker=-stfail` and `--broken-marker=-hang` need the `=`. The simulator's `--confirm-timeout` is its own timer, not the stage's `confirm_timeout_s`. Do not name the module `selftest.py` (`broker`/`storage selftest` and `deploycheck` use the word for infrastructure probes).

---

## 2026-10-16 — The safe window: ff_set_safe_window_cb()/Fleetforge.onSafeWindow() gate every agent reboot; an unsafe verb is answered deferred and reports awaiting_safe_window; a deadline or self-test rollback is latched on time and only its reboot waits, in a static gate task, never on esp_timer; a deferred apply stays cancellable; built unreleased (R5-fw-5)

- **Built.** `include/ff_safe_window.h` (maker API, additive-only from the release that ships it), `src/ff_safe_window.c` + `_internal.h` (registration, freeze, `ff_safe_window_registered()` for any context, `ff_safe_window_is_safe()` for tasks, `FF_SAFE_WINDOW_POLL_MS 250`). `ff_mqtt.c` (CRITICAL): the freeze + `safe_window` capability in `ff_mqtt_run()`, `start_rollback_gate()`/`rollback_gate_task` (static, priority 6), the persistent notify-woken `ff_reboot` task with `wait_for_the_window()` and the hold/abort/commit handshake under `s_reboot_lock`, `deferred` answers for apply/rollback/reboot, the cancel of a deferred apply, a stage refused while a reboot is pending or a rollback decided. `ff_ota.c`/`.h` (CRITICAL): `FF_OTA_TXN_AWAITING`, the apply:auto wait, cancel checkpoint 6, the re-check before `esp_restart()`. `Fleetforge.cpp`/`.h` (CRITICAL): `onSafeWindow()` + the `ff-safe` tag; nothing in `begin()`, `verifyRollbackLater()` or the `#error` changed. `FF_STATUS_AWAITING_SAFE_WINDOW` + `FF_DETAIL_APPLY_WAITS`/`FF_DETAIL_ROLLBACK_WAITS`. Both worked examples register a rule (reboot only between two messages). `lib_quickstart.py` 2a requires `safe_window`. No spec, server, simulator, frontend, `fleetforge_start.c`, `agent_main.c`, partition or sdkconfig change; no version bump. Details: `docs/features/self-test.md` -> *R5-fw-5 as built*; runbook `docs/runbooks/agent-qemu.md` -> *The safe window (R5-fw-5)*.
- **Decisions.**
  - (1) **Unsafe longer than the confirm window (the TODO's question, D12):** the deadline decides on time (the `s_decide_lock` latch, the stored reason), the image is never confirmed afterwards (a late ack or self-test pass logs `NOT confirming`), and only the reboot waits, with no deadline on the wait. Mark-invalid stays at the end (the image is PENDING_VERIFY, so a reset during the wait returns the board anyway).
  - (2) **Never on esp_timer.** `confirm_timeout_cb` (after its claim and both unchanged log lines), the backstop and `selftest_done`'s fail hand the reboot to `start_rollback_gate()` when a rule is registered. The gate never touches the confirm timer (D10). With no rule the esp_timer path is the R5-fw-4 one line for line, so the stock agent is timer-identical.
  - (3) **`awaiting_safe_window` only when the first evaluation says unsafe**; a safe moment goes straight on. The rule is asked once per verb, after every refusal (rollback: after its mark-invalid); repeats and no-ops stay `accepted`.
  - (4) **Command reboots**: one static `ff_reboot` task, created once and woken with `xTaskNotifyGive` (a cancelled deferred apply returns it to idle; a static TCB is never re-created). Drain < window < log < `esp_restart()` kept; the rule is asked outside the lock, and a commit happens under `s_reboot_lock` only without hold or abort.
  - (5) **Cancel of a deferred apply (D4)**: hold, the single `ff_ota_cancel()`, then abort only on ESP_OK. An apply answered `accepted` stays uncancellable. apply:auto waits in `ota_task` in AWAITING, cancellable until checkpoint 6 commits APPLYING.
  - (6) **A stage is refused while a reboot is on its way** (R5-fw-4 gap (b), now unbounded): `failed` `a reboot is already pending` / `a rollback is already decided`, after the re-delivery drop.
  - (7) **Poll 250 ms**, not 1 s: the examples' window is the 800 ms gap between messages.
  - (8) **Deviations from the plan.** A `rollback` command's claim also sets `s_rollback_commanded`, and the backstop then starts no gate (the command's task already waits; a second party would repeat statuses and mark an already-marked image). The gate waits the 2 s grace only when it queued `rolling_back`. A deferred plain `reboot` has no second drain.
  - (9) **Tests changed on purpose.** `test_ff_cfg.py::test_the_walk_the_agent_performs_is_declared` (+`awaiting_safe_window`); `test_agent_verbs.py` (checkpoint 4 assigns AWAITING, the staged exit takes AWAITING, the enum order, the `safe_window` pin turned positive); `test_selftest.py` (`ff_safe_window_is_safe` in `TIMER_FORBIDDEN`, `start_rollback_gate` a timer-context function, the freeze order); `test_ota_component.py` (`MAKER_HEADERS`, `INTERNAL_HEADERS`, `HAZARDS`); `APP_SIZE_BUDGET_BYTES` (+2,960 to +3,376 B).
- **Rejected.** The rule on esp_timer; one task shared by the command reboot and the gate; a dynamic gate task; pausing/extending the deadline; a deadline or timeout on the wait; `force`; the unregistered path through the gate; repeats answered `deferred`; `awaiting_safe_window` when the first evaluation is safe; marking invalid at decision time; a test-only build flag for the window.
- **Named gaps.** An `accepted` verb whose window closes during its drain waits with `applying`/`rolling_back` already said (logged only); a rule stuck at unsafe holds the board until a reset (which returns it); a hog above priority 6 starves the gate the same way; the backstop can start the gate before a starved self-test stored its fail head (`selftest timed out`); a reset during a pre-apply wait boots the staged image (unchanged); a cancelled deferred apply's id stays the remembered reboot id; without a rule the backstop still reboots at once inside a `rollback` command's drain (R5-fw-4; its second mark-invalid fails harmlessly).
- **Proposed spec addenda** (owner; Patch A2/A4/A5): A2 — "A `stage` that arrives while a reboot waits for the safe window, or after the device has decided to roll back, is refused." A4 — "The device reports `awaiting_safe_window` only when it judges the moment unsafe; otherwise it goes straight on." A5 — "A command that repeats one already accepted or deferred is answered `accepted`; only the command that starts a wait is answered `deferred`."
- **T2 evidence** (dev stack 8088, api on 10.0.2.2 origins, restored afterwards; Arduino variants W `millis() % 80000 >= 60000` and L `millis() > 400000` with the self-test answered after 320 s; millis estimated from the wall clock, see Gotchas). A: `capabilities [...,"selftest","safe_window"]`, the registered line before `connecting to`. B `apply` unsafe → `deferred` + `awaiting_safe_window` `apply waits for a safe window`, no `applying` for 18 s, then `applying`, `rebooting`, `rebooting for command fw5-ap1-…` ~250 ms + 1.5 s after the window; cold boot B `CONFIRMED`, `selftest passed`. C `apply` `deferred`, `cancel` → `accepted`, put back, `cancelled`, `the deferred apply was cancelled — no reboot`, no reboot through the next safe stretch. D `apply: auto` → `staged`, `awaiting_safe_window`, `applying`/`rebooting` at the window, D `confirmed`. `reboot` → `deferred`, `cmd_id` null, no status; a stage meanwhile → `failed` `a reboot is already pending`, nothing downloaded; `rebooting for command` at the window. `rollback` → `deferred` (own id) + `awaiting_safe_window` `rollback waits for a safe window`, `rolling_back` at the window, D `rolled_back` `rolled back by command; returned to ota_0`. L (D12): `no self-test result 300 s` → `the rollback is decided and waits for a safe window` → `awaiting_safe_window` → late pass `NOT confirming` → `a safe window: rolling back now` ~260 ms after millis 400000 → `rolling_back` → D `rolled_back` `selftest timed out; returned to ota_0`; never confirmed; ota_1 INVALID. Regressions: `just lib-quickstart` `== PASS (2375 s)`: 2a capabilities include `safe_window`; 2c/2d/2e roll back through the gate (steps `… confirming, awaiting_safe_window, rolling_back, rolled_back`) with the R5-fw-4 details; the stock agent logs `no safe-window rule registered…` and announces no `safe_window`; `just agent-qemu-smoke esp32`: `HARNESS OK`.
- **Mutation checks** (each red, then reverted): the rule called in `confirm_timeout_cb`; the backstop's hand-off dropped; checkpoint 4 back to APPLYING; the commit without `s_reboot_hold`; the `on_stage` refusal removed; `esp_timer_stop(s_confirm_timer)` in the gate.
- **Gotchas.** QEMU's log timestamps are FreeRTOS ticks and run 5-8 s ahead of `millis()` (esp_timer follows the host clock), so window timing was measured from the wall clock since the ROM reset line. The command-reboot soft reset hit the `esp_timer_impl_init` panic loop again; a PENDING_VERIFY image survives it only if QEMU is killed right after `rebooting for command` (226 ms worked) or during ff_ota's 1.5 s drain. After a staged cancel the running entry is NEW, so a rollback to it is refused until a power cycle re-confirms it (R5-fw-4 gap (a)); T2 re-confirmed B by a stage. The server does not know `cancelled` as terminal (R5-be-2): redeploying a cancelled version reuses its `cmd_id`, so T2 used a fresh artifact. One `lib-qemu --fresh` under a TTY panicked in `task_wdt_isr` before `app_main` (the known boot-loop panic); the same bundle then booted. BSS rose by the gate's static 4 KB stack and TCB (the reboot task's were already there).

---

## 2026-10-16 — The four verbs: apply, cancel, rollback and reboot are carried out and answered once on up/ack (never retained, never up/status); a reboot-causing verb reboots from a static task 1.5 s after its delivery and keeps its id in NVS; cancel is decided under one lock in ff_ota; rollback only to a VALID/UNDEFINED slot; built unreleased (R5-fw-4)

- **Built.** `ff_mqtt.c` (CRITICAL): `topic_ack` + `enqueue_ack()` (QoS 1, retain 0, A5 key order, explicit nulls), the ack vocabulary (`#define`s, each `_Static_assert`ed <= 63 B), `on_apply`/`on_cancel`/`on_rollback`/`on_reboot`, `refused`/`not implemented` for every other id'd type, `schedule_command_reboot()` with a static `ff_reboot` task, the persisted reboot-id dedupe, `other_slot_is_returnable()`, and `classify_txn` composing `rolled back by command; returned to <slot>`. `ff_ota.c`/`.h` (CRITICAL): `ff_ota_txn_phase()`, `ff_ota_cancel()`, `s_cancel_lock` with five checkpoints, `restore_boot_partition(why)` returning bool. `ff_txn`: `ff_txn_note_reboot_cmd()`/`ff_txn_last_reboot_cmd()` in namespace `ff_cmd`. `FF_STATUS_CANCELLED` ahead of Patch A3. Capabilities `["ota","apply","cancel","rollback","reboot"]` (+`selftest`). No spec, server, simulator, frontend, `Fleetforge.cpp`, `fleetforge_start.c`, `agent_main.c`, partition or sdkconfig change; no version bump. Details: `docs/features/self-test.md` -> *R5-fw-4 as built*; runbook `docs/runbooks/agent-qemu.md` -> *Driving the verbs (R5-fw-4)*.
- **Decisions.**
  - (1) **Answers.** One per verb on `up/ack`, enqueued from the handler; `cmd_id` = the transaction acted on, or on a refusal the one the device holds that caused it, else null. The three spec refusals verbatim; twelve more agent texts (`nothing to cancel for this cmd_id`, `already applying; use rollback`, `a rollback is already decided`, `an update is in progress`, `the running image is not confirmed yet`, `a reboot is already pending`, `cmd_id missing`, `command id too long`, `the image is confirming right now`, `cannot mark this image invalid`, `cannot put the boot slot back`, `not implemented`) are text for a server to show, not a contract. `refused` = nothing changed: every refusal precedes any status, record write, mark or reboot (the one exception, `cannot mark`, reverts its record first).
  - (2) **D8.** The verb handler never reboots: `schedule_command_reboot()` runs last, persists the id (`ff_cmd`/`reboot_id`, NVS: RTC dies at a power cut and at every QEMU stop), and starts `ff_reboot` with `xTaskCreateStatic` (4 KB static stack, priority 3), which sleeps `COMMAND_REBOOT_DRAIN_MS` 1500, logs `rebooting for command <id>`, `esp_restart()`. R5-fw-5's safe-window poll goes in that task. The id is loaded before `classify_txn()` and the first connect; a command with it is dropped unanswered (`… already rebooted for it`).
  - (3) **Cancel.** ff_ota owns it: under `s_cancel_lock`, an in-flight transaction gets a flag the ota task acts on at its next checkpoint (before `begin`, every perform pass, before `finish`, after `finish` before the record, the staged exit) and reports `cancelled` itself; a staged image whose task has ended is cancelled inline and the session enqueues `cancelled`. `s_running` goes false in the same critical section that reads the flag at `done:`, so a cancel is never accepted and dropped or acted on twice, and never accepted once `applying`. Staged cancel = put-back FIRST, record cleared only if that worked.
  - (4) **Rollback.** Returnable = other slot VALID or UNDEFINED and `esp_ota_check_rollback_is_possible()` (NEW, PENDING_VERIFY, INVALID, ABORTED, no entry refused). An unconfirmed image claims the R5-fw-2 latch (a self-test pass that claimed first wins), stores the head over the transaction's, marks invalid (no reboot variant) and reboots via the task; a confirmed image opens its own record `(id, running, 0)`, marks, and on a mark failure clears the record and refuses. Order of checks: already on its way → accepted; reboot pending → refused; update in hand → `cancel first`; returnable.
  - (5) **Apply** releases STAGED only: `accepted`, `applying`, `rebooting`, reboot (D3); past `staged` = accepted no-op; anything else (also before `staged`) = `nothing staged for this cmd_id`. **Reboot** refused while a rollback is decided, an update is in hand, or the image is unconfirmed.
  - (6) **Deviations from the plan.** STAGED outside the ota task comes from otadata + the ff_txn record (slot and `cmd_id`), not a RAM phase (a refused second stage reuses `s_running_cmd_id`); `ff_ota_txn_phase()` returns the held id through an out-parameter; a NULL query answers TRANSFER until the ota task ends even with a cancel pending; a put-back that fails after `finish()` does not report `cancelled` (the update carries on, record and all); UNDEFINED is IDF's "state never set" entry, while a USB-flashed slot with blank otadata has no entry and IDF cannot roll back to it either.
  - (7) **Tests changed on purpose.** `test_ff_cfg.py::_spec_states` unions Patch A3's proposed line while the spec lacks `cancelled` (self-switching, the test_telemetry idiom) and `cancelled` joins the declared walk; `test_selftest.py::test_only_the_session_stores_a_detail` 2 → 3 (`store_rollback_head`); `APP_SIZE_BUDGET_BYTES`.
- **Rejected.** Rebooting in the handler or from esp_timer; RTC memory for the id; `xTaskCreate` for the reboot task; an apply-on-arrival latch; acks on `up/status`; erasing the cancelled slot or the inactive otadata sector; `esp_ota_mark_app_invalid_rollback_and_reboot()` in a verb; announcing `safe_window`; a build flag for the verbs.
- **Named gaps.** The re-confirm boot after a staged cancel (and a fallback to the cancelled NEW entry if that boot fails); a `stage` inside the 1.5 s drain is not refused (fw-5); a redelivered stage with a cancelled id runs again (R5-be-2); an own-transaction `rolled_back` has no `requested` row (R5-be-2); an ack queued while disconnected dies with the reboot; a rollback of a confirmed image overwrites a record whose outcome was still owed; the staged cancel's flash writes run in the handler (7.8 s in QEMU).
- **Proposed spec addenda** (owner; Patch A2/A5): `apply` — "An `apply` that arrives before its transaction reaches `staged` is refused; a server sends it after `staged`." `cancel` — "A `cancel` that arrives while the device is verifying is carried out even if the boot slot has just moved; the device moves it back before `staged`." A5 — "The refusal `detail` texts are not a contract; a server shows them as text."
- **T2 evidence** (dev stack 8088, api on 10.0.2.2 origins, restored afterwards; A `--fresh`, B 0.5.9, R rbtest; commands as `ff-commander`). Capabilities `["ota","apply","cancel","rollback","reboot"]`. `apply` with `cmd_id` `nope-…` → `{"id":"fw4-ap0-1792143537","type":"apply","result":"refused","cmd_id":null,"detail":"nothing staged for this cmd_id"}`, no status, no reboot, not retained (a fresh `up/ack` subscriber got nothing). `identify` → `not implemented`. `reboot` → `accepted`/null, `rebooting for command fw4-rb1-…` 1.5 s after the id was stored, `rst:0xc`, `Loaded app … 0x20000`; after a stop/start the same id → `duplicate … already rebooted for it`. Cancel in `downloading` → `accepted`, `cancelled`, otadata untouched. Cancel of a staged image → `boot partition put back to ota_0: the update was cancelled by command …`, `record cleared`, `accepted`, `cancelled`; the next stage was not refused; the power cycle re-confirmed ota_0 (`pending_verify` → `CONFIRMED`, no transaction line). While staged: `reboot` → `an update is in progress` and `rollback` → `cancel first`, both with the stage's `cmd_id`; `apply` → `accepted`, `applying`, `rebooting`, `Loaded app … 0x200000`. Rollback on confirmed B → `accepted` with its own id, `rolling_back`, back on ota_0 `rolled_back` `rolled back by command; returned to ota_0`, ota_1 INVALID; again → `no confirmed image to return to`. Rollback while R confirms → `reboot` refused `the running image is not confirmed yet`, rollback `accepted` with R's `cmd_id`, `rolling_back`, never `no working session`, A reports the same detail, ota_1 INVALID. `just lib-quickstart`: `== PASS (2383 s)`: 2a `capabilities ['ota', 'apply', 'cancel', 'rollback', 'reboot', 'selftest']`; 2b `confirmed`, detail `selftest passed`; 2c `no working session 60 s`, `rolled_back` `returned to ota_1; ota_0 did not confirm`; 2d `selftest failed: i2c sensor 0x48 did not ans; returned to ota_1` (63 B); 2e `selftest timed out; returned to ota_1`; credentials in 0 of 29 transcripts. `just agent-qemu-smoke esp32`: `HARNESS OK`.
- **Mutation checks** (each red, then reverted): `esp_restart()` in `on_reboot`; the staged cancel clearing before the put-back; NEW accepted as returnable; the pre-finish cancel check dropped; the ack retained.
- **Gotchas.** QEMU's soft reset panicked (`esp_timer_impl_init`) after every command reboot in this run, so a command-applied B was aborted by the bootloader and confirmed by a power-cycle instead; the proof of each reboot is `rebooting for command`, one `rst:0xc` and the `Loaded app` offset. `esp_ota_begin()` invalidates the inactive otadata sector. `APP_SIZE_BUDGET_BYTES` rose 5.9-6.6 KB per target. The pinned first-occurrence literals were not repeated (`const bool decided = s_rollback_decided;`). `_function_body` cannot see a function whose parameters contain `)`; all new ones are plain.

---

## 2026-10-16 — The confirm deadline is the stage's confirm_timeout_s, clamped 30-3600 s and counted from image start: the boot arm stays 300 s, and the transaction record re-arms it; an FF_ROLLBACK_TEST image caps it at 60 s; built unreleased (R5-fw-3)

- **Built.** `on_stage()` resolves `confirm_timeout_s` (clamped 30-3600 s; 300 when absent or not a number) into `ff_ota_cmd_t.confirm_timeout_s`; `ff_ota.c` passes it to `ff_txn_save()` unread; `ff_txn` gains an optional u32 key `confirm_s`. The first-statement boot arm is unchanged (300 s, no NVS) but now keeps its handle, origin and deadline; `classify_txn()`, in the `TXN_CONFIRMING` branch only, calls the new `rearm_confirm_deadline()` with the record's value clamped again by the new image. No spec, server, `Fleetforge.cpp`, `fleetforge_start.c`, `agent_main.c`, partition or sdkconfig change; no version bump. Details: `docs/features/self-test.md` -> *R5-fw-3 as built*.
- **Decisions.**
  - (1) **The value lives in `ff_txn`** as an optional key, read only after the record is judged complete (the `detail` rule). 0 = not stored (pre-R5-fw-3 record) = the default.
  - (2) **`ff_mqtt.c` clamps, twice**: at the stage with the stager's limits, and at classify with the new image's limits. `confirm_timeout_clamp(double)` compares before it casts (NaN/negative/0 -> 30; > max, inf -> max; fractions truncate). Warnings never use `%f`.
  - (3) **The re-arm** is `esp_timer_stop()` + `esp_timer_start_once()` for `deadline − (now − origin)`, clamped at 0. Same deadline: no timer touch (`confirm deadline stays N s`), so the default 300 s and the rbtest 60 s paths are timer-identical to R5-fw-2. A failed stop means the timer fired: its decision stands. The new deadline is stored between stop and start. The start is `ESP_ERROR_CHECK`ed (main task; an abort in PENDING_VERIFY rolls back, the boot arm's posture).
  - (4) **Only `TXN_CONFIRMING` re-arms.** TXN_NONE + PENDING_VERIFY keeps 300 s; BOOTED_NEW gets no timer.
  - (5) **`FF_ROLLBACK_TEST` caps a command at 60 s** (`CONFIRM_TIMEOUT_MAX_S`, inside the existing fault-injection block): the server always sends 300, so phase 2c and the runbooks stay at 60 s, and a shorter command gives its own value. Extends the one documented fault-injection switch; not a new test-only override. `_Static_assert` keeps the default inside the clamp.
  - (6) **The log lines** keep their format strings byte for byte; `confirm_timeout_cb` and the rbtest hook line print `s_confirm_deadline_s` (volatile u32), the boot-arm line still prints `CONFIRM_TIMEOUT_S`.
  - (7) **Save order** erase -> target -> `confirm_s` -> `cmd_id` -> commit: a complete record always carries its deadline.
  - (8) **Not paused while unsafe (D10)**: the safe-window wait (R5-fw-5) must never stop, extend or re-arm this timer.
- **Rejected.** `esp_timer_restart()` (IDF 5.5 tests `armed` before taking the list lock: it can re-insert a just-dispatched timer and run the callback twice, the second run rebooting via the backstop without the `rolling_back` report). A re-arm in `agent_main.c`/`fleetforge_start.c`/`Fleetforge.cpp` (CRITICAL files; the R2-fw-4 parity tests). An app-side deadline for BOOTED_NEW (needs its own decision). An uncapped rbtest image. Clamping in `ff_ota.c`. A compose passthrough or REST field for the server's value (out of scope).
- **Named gaps.** (a) An image that hangs before `ff_mqtt_run()` keeps the 300 s boot arm (the record is read by `classify_txn()` only); a longer command then rolls back early, the safe direction. (b) BOOTED_NEW still has no deadline. (c) The simulator keeps its own `--confirm-timeout`.
- **Proposed spec addendum** (owner; Patch A4, *The confirm deadline*): "Until the device has read its transaction record, which happens once its network is up, it uses 300 s."
- **T2 evidence** (dev stack 8088, api on 10.0.2.2 origins, restored afterwards). A stages R (`0.5.0-rbtest`) from a hand-built `fw3-30s-1792133121` with `confirm_timeout_s: 30`: `ff-txn: transaction fw3-30s-1792133121 recorded (target slot at 0x00200000, confirm 30 s)`, `ota_1 is staged and bootable`. After the power cycle R logs `W (2899) OTA boot: 60 s` -> `W (12609) … confirming on ota_1` -> `W (12619) … confirm deadline 30 s from image start (20 s left)` -> `E (12849) FF_ROLLBACK_TEST: … roll back in 30 s` -> `E (33039) no working session 30 s after an OTA boot` (30.14 s after the arm); never `60 s`, never CONFIRMED. otadata ota_1 `INVALID`; A on ota_0 reports `rolled_back (returned to ota_0; ota_1 did not confirm)`. A `confirm_timeout_s: 5` stage is clamped at A (`outside 30-3600 s — using 30 s`, `confirm 30 s`) and R fires at 33495 (30.19 s after its arm at 3305), ota_1 `INVALID`. `just lib-quickstart`: `== PASS (2303 s)`, every phase as in R5-fw-2 (2c R `no working session 60 s` with the server's 300, `rolled_back`; 2e T `no self-test result 300 s`, `selftest timed out; returned to ota_1`). `just agent-qemu-smoke esp32`: `HARNESS OK`. The plan's 29500-33000 ms window assumed the deadline starts at log time 0; it starts at `app_main` (2.9-3.3 s of QEMU boot), so both fires are 30.1-30.2 s after the arm and a few hundred ms past 33000 of log time.
- **Mutation checks** (each red, then reverted): the record used raw without the load-time clamp; `esp_timer_restart` instead of stop+start; the remaining time from `esp_timer_get_time()` alone; the confirm key written after `cmd_id`.
- **Gotchas.** `APP_SIZE_BUDGET_BYTES` rose 1.2-1.3 KB per target, mostly log strings. In both QEMU runs R's `esp_restart()` did not panic: the emulator booted ota_0 on its own, so no stop/start was needed for the rollback (otadata is still read after a stop). Pinned first-occurrence literals were not repeated in new code, and `confirm_timeout_cb` gained no `esp_timer_start_once(`.

---

## 2026-10-16 — The confirm waits for the self-test: the announce ack starts it, only a pass confirms, a fail rolls back at once with its reason stored in ff_txn, and the deadline still applies; built unreleased (R5-fw-2)

- **Built.** `ff_mqtt.c` (CRITICAL). On the announce ack of a boot that needs confirming (`pending_verify()` or `TXN_BOOTED_NEW`), an image with a registered self-test runs `ff_selftest_run(selftest_done)` instead of confirming. It is one `else if` between `if (s_rollback_decided)` and the unchanged R2-be-1 confirm line, and with no self-test the ack path is unchanged. `ff_txn` gains an optional `detail` key plus `ff_txn_set_detail()` (cmd_id-checked, never erases). `ff_identity` announces `selftest` when one is registered (D9). Stale "Until R5-fw-2" text was removed. `scripts/lib_quickstart.py` gains builds F (the self-test fails) and T (it never answers) and `phase_selftest_rollback`. No spec, server, `Fleetforge.cpp`, `fleetforge_start.c`, partition or sdkconfig change; no version bump. Details: `docs/features/self-test.md` -> *R5-fw-2 as built*.
- **Decisions.**
  - (1) **A decision latch.** `s_decide_lock` (portMUX) plus `s_confirm_claimed` and the existing `s_rollback_decided`. The self-test task and the confirm deadline each claim under the lock, and the first claim wins. Mark-valid, mark-invalid and NVS writes happen outside the lock. A deadline that finds a failed self-test's claim is the backstop and reboots at once.
  - (2) **The guard.** `if (!pending_verify()) return;` plus `if (s_ctx.session_confirmed && !s_selftest_registered) return;`. `s_selftest_registered` is set from the freeze in `ff_mqtt_run()`, before the first connect. The old log line is kept byte for byte, and a held image logs `no self-test result %d s …`.
  - (3) **The head is stored by the image under test, and the returned-to image appends `; returned to <slot>`.** `selftest timed out` is pre-stored at the ack (`TXN_CONFIRMING` only), STRICTLY BEFORE the run, so the esp_timer path never writes NVS. A reset or crash mid-self-test therefore reports `timed out`, which is intended. `selftest failed: <reason>` is stored at decision time, before the grace timer. There is no pre-store before the ack, so a timeout before the ack keeps `returned to X; Y did not confirm`. Only the reason is shortened (27 B for `ota_N`); an empty reason becomes `no reason given`.
  - (4) **`classify_txn`** composes only under the `selftest ` prefix. A stored head plus VALID at the target repeats `selftest passed`.
  - (5) **DECIDED, `TXN_BOOTED_NEW` plus a self-test.** It runs. A fail calls app-side `esp_ota_mark_app_invalid_rollback_and_reboot()`: IDF 5.5 never boots an INVALID entry, armed or not, and `failed` would leave a self-declared-broken image running with the target in `fw_version`. The self-test detail wins on a fail. A pass gives `confirmed` with `the bootloader never armed rollback for this image` (DETAIL_BOOTED_NEW wins on a pass: the one sign the board cannot roll back by time). This deviates from Patch A's "`confirmed` then carries `selftest passed`". **Proposed addendum** for Patch A4's *Custom self-test* paragraph (owner): "An image the bootloader never put in pending-verify keeps its `confirmed` detail `the bootloader never armed rollback for this image` when its self-test passes, and rolls itself back when it fails."
  - (6) **A runner that cannot start** keeps the hold (fail-safe): the deadline rolls back with the pre-stored `timed out`.
  - (7) **Accepted races.** `s_txn.kind`/`terminal_msg_id` are written from the self-test task on a pass (aligned int stores). The worst case is a missed clear or a doubled `confirmed`, and the server deduplicates.
- **Rejected.** Handing the result to the mqtt task with `esp_mqtt_dispatch_custom_event()` (with queue size 1 it is drained only by another mqtt event, so it stalls while disconnected). Writing NVS from the esp_timer path. `failed` for BOOTED_NEW. Setting the gate flag at the ack (a window with no deadline). Refactoring `confirm_timeout_cb`'s grace/report into a shared helper (it would move the pinned `esp_timer_start_once(` out of its body). A test-only confirm-timeout override (R5-fw-3 makes it per-command).
- **T2 evidence.** `just lib-quickstart --keep` gave `== PASS (2308 s)`; the kept tree was deleted afterwards.
  - 2a: `capabilities ['ota', 'selftest']`.
  - 2b: B logs `running the custom self-test` (12600) → `custom self-test passed` (20181) → `CONFIRMED` (20463). The row: `confirmed`, detail `'selftest passed'`.
  - 2c: R is unchanged (`rolled_back`, `returned to ota_1; ota_0 did not confirm`, no run line).
  - 2d: F acks, then `ff-txn: … detail stored` (28589, the pre-store) before `running the custom self-test` (28609). Then `custom self-test failed: i2c sensor 0x48 did not answer tried 3 times`, the fail head stored (29356), `the custom self-test failed — marking this image invalid…`, `rolling_back` queued, `SW_CPU_RESET`, never CONFIRMED. After the power cycle B logs `rolled_back (selftest failed: i2c sensor 0x48 did not ans; returned to ota_1)` and `morse: HELLO`. The row: `rolled_back`, terminal, `fw_version` B, detail exactly that string (63 bytes).
  - 2e: T logs `no self-test result 300 s after an OTA boot` at 310100 ms. The row: detail exactly `selftest timed out; returned to ota_1`.
  - The stock agent: `HARNESS OK`. A fresh `agent-qemu --fresh` logs `no custom self-test registered…`, and its row has `capabilities ['ota']`.
- **Mutation checks** (each red, then reverted): the pre-store after the run; `!s_selftest_registered` dropped from the guard; the claim/lock dropped from `selftest_done`; classify ignoring `rec.detail`.
- **Gotchas.** `APP_SIZE_BUDGET_BYTES` rose 3.4-3.9 KB per target: the ack branch references `ff_selftest_run()`, so the runner is no longer garbage-collected from the stock agent. The pinned literals (`else if (confirm_this_image() && s_txn.kind == TXN_CONFIRMING)`, `s_txn.kind == TXN_BOOTED_NEW && accept_unverified_image()`, `if (s_rollback_decided)`) are found by `str.index()` on their FIRST occurrence, so `selftest_done` spells its logic differently (a local `kind`, `too_late`). The Arduino bootloader does not log `Loaded app from partition`, so "back on B" rests on B's morse firmware string and the row's `fw_version`. BOOTED_NEW, the late-answer race and the backstop are not reachable in QEMU (its bootloader always arms); they are covered by the source pins only.

---

## 2026-10-15 — The self-test entrypoint: ff_set_selftest()/ff_selftest_report() and Fleetforge.onSelfTest()/selfTestDone(), registered right after begin() and frozen when the session starts; built unreleased, not yet run (R5-fw-1)

- **Built.** `include/ff_selftest.h` (maker API, additive-only from the release that ships it), `src/ff_selftest.c` + `src/ff_selftest_internal.h` (registration, freeze, result latch, and the runner `ff_selftest_run(done)`: its own task `ff_selftest`, 6144 B, priority 1, created on demand, deleted after `done`), one `(void)ff_selftest_freeze();` in `ff_mqtt_run()` before `ff_txn_init()` and the first connect, `Fleetforge.onSelfTest()`/`selfTestDone()` and the `ff-selftest` log tag in `Fleetforge.cpp`, both worked examples, the library README. Nothing calls `ff_selftest_run()` yet; no confirm, rollback, wire, server or spec change; no version bump. Details: `docs/features/self-test.md` -> *R5-fw-1 as built*.
- **Signatures and why.** C keeps the feature doc's three-value return with a zeroed reason buffer (>= 28 B), plus `ff_selftest_report()` for a pending answer: sync and async in one contract. Arduino gets a no-argument callback and `selfTestDone(passed, reason = nullptr)`: one result path for a beginner API (the trampoline always says PENDING; an answer given inside the callback has already won). The enum values (PASS 0, FAIL 1, PENDING 2) are explicit because they are ABI once released. First answer wins; anything but PASS/PENDING is FAIL; the reason is sanitized to printable ASCII without `;`; `ff_selftest_report()` never logs, allocates or blocks.
- **Registration point.** The TODO's "register before `fleetforge_start()`/`begin()`" is read as "before the session": `begin()`/`fleetforge_start()` FIRST is the CRITICAL rollback posture (confirm timer armed before anything else), pinned by `test_the_example_calls_begin_first` and `test_app_main_calls_fleetforge_start_first`, which are unchanged. The examples register as the SECOND statement; `ff_mqtt_run()` freezes the registration before its first connect (after link and SNTP), and a later one is a deterministic refusal (`ESP_ERR_INVALID_STATE` / `false`, logged). This is the precedent for fw-5 `Fleetforge.onSafeWindow` and fw-7 `setPowerClass`.
- **Not yet.** No `selftest` capability in the announce until the hold is real (fw-2): D9 reads to a server as "this image confirms only after its self-test". The runner is built and unwired until fw-2.
- **Test-helper fix.** `tests/test_ota_component.py::_declared_functions` now removes function-pointer typedefs before matching `ff_*(`: otherwise `typedef ff_selftest_result_t (*ff_selftest_fn)(...)` made `ff_selftest_result_t` a "function" both examples would have to call. No other public header has one. `MAKER_HEADERS = ("ff_selftest.h", "ff_telemetry.h")`.
- **Rejected.** Examples registering before `begin()`/`fleetforge_start()`; an Arduino callback returning the enum; running the function in the esp-mqtt handler, on esp_timer, or synchronously in `ff_selftest_run()`; a task notification to wake the runner (races the task's exit, use-after-free; a static binary semaphore cannot be freed); logging or blocking in `ff_selftest_report()`; announcing `selftest` now; wiring the runner "advisory" after the confirm; a QEMU-only probe flag; raising `ONE_SCREEN`.
- **T2 evidence.** Arduino Basic in QEMU: `ff-selftest: a custom self-test is registered for this image` once, at line 44, before `ff-mqtt: connecting to` (45), then `announce acknowledged` and 4 `morse: SOS` lines in 20 s; judge `SELFTEST REGISTRATION OK` (no run/pass/fail, no refusal, no panic, one boot); the device's `capabilities` stay `["ota"]`. Stock agent: `no custom self-test registered: an OTA'd image confirms once its announce is acknowledged` before `connecting to`; `HARNESS OK`. OTA path unchanged: `just lib-quickstart` `== PASS (1754 s)`, build B confirmed on the announce ack, build R `-rbtest` rolled_back (`returned to ota_1; ota_0 did not confirm`); a `--keep` rerun shows the registered line once in each of the four QEMU transcripts (A, B, R, R2) and no run line.
- **Gotchas.** `tests/test_agent_txn._function_body` cannot match a C++ member whose parameter list holds `)` (`void (*test)()`); `test_selftest.py` has its own `_member_body`. The arduino-package test reads git: `git add` new files under `include/`/`src/` before `just test`. The dev `.env` admin password is quoted.

---

## 2026-10-15 — R5's wire is proposed, not applied: verbs with their own id answered on a new up/ack topic, a cancelled state, a capabilities vocabulary, a 30-3600 s confirm deadline, a self-test detail grammar, a latched rollback that waits for the safe window, and a sleepy device that stays awake through its confirm (R5-spec-1)

- **Proposed** (paste-ready: `docs/features/self-test.md` -> *R5 wire and API proposal (R5-spec-1, proposed)*; all additive, `proto` stays 1). (D1) `apply`/`cancel`/`rollback`/`reboot` each carry their own `id`; `apply` and `cancel` also carry `cmd_id`, the stage's id, and are refused for any other transaction. (D2) Answers on a new topic `up/ack` (QoS 1, not retained): `{"id","type","result","cmd_id","detail"}`, `result` `accepted`/`deferred`/`refused`, `detail` <= 63 B, never a URL; the server keeps the first answer per `id` and allows for none (older agents are silent). (D3) `apply` releases a `staged` `on_command` transaction, which then walks like `auto`; past `staged` it is a no-op, with nothing staged it is refused. (D4) `cancel` up to a pre-apply `awaiting_safe_window`: transfer stopped, boot pointer back on the running image, new terminal state `cancelled`. (D5) `rollback` ends a `confirming` or waiting-to-roll-back transaction, otherwise opens its own under its `id`; only to an image that booted and was confirmed in that slot or was USB-flashed there; refused mid-update. (D6) `reboot` opens no transaction and is refused between `staging` and `confirmed`. (D7) Every agent-initiated reboot waits for the safe window; no `force`. (D8) A reboot-causing verb acts only after its delivery returns, and its id survives the reboot. (D9) Capabilities vocabulary `apply`, `cancel`, `rollback`, `reboot`, `selftest`, `safe_window`; a server sends a verb or `apply: "on_command"` only to a device that lists it. (D10) `confirm_timeout_s` from the stage, kept with the transaction, from image start, clamped 30-3600 s, absent = 300 s, not paused while unsafe. (D11) Self-test after the announce ack; `selftest passed` / `selftest failed: <reason>; returned to <slot>` / `selftest timed out; returned to <slot>`, the whole detail <= 63 B (the agent's `detail[64]` minus the terminator; 27 B of reason worst case), the reason stored in `ff_txn` at decision time. (D12) Deadline or self-test failure while unsafe: the rollback decision is latched, only the reboot waits, `awaiting_safe_window` with `rollback waits for a safe window`, no deadline on the wait; no predicate = always safe. (D13) `safe_mode` announce field, prose only. (D14) A sleepy device does not sleep from `staging` to `rebooting` nor on an unconfirmed image. API names (feature doc only): `ff_set_selftest`/`ff_selftest_report`/`Fleetforge.onSelfTest`/`selfTestDone`, `ff_set_safe_window_cb`/`Fleetforge.onSafeWindow`, `Fleetforge.isConnected`/`isReadyForSleep`/`setPowerClass`. Nothing under `spec/`, `src/`, `agent/`, `tests/` or `alembic/` changed.
- **Why `up/ack`, not `up/status`.** `ingestor/handlers.py` records every `up/status` with a `cmd_id` into `deploy_events`, even with no `requested` row, and `failed` is terminal: a refused `apply` reported there would be a terminal failed deploy in both KPIs. An unknown `up/` channel only moves `last_seen` today, and the ACL `up/#` pattern already covers it.
- **IDF 5.5 facts the text relies on.** The bootloader ABORTs any entry still `PENDING_VERIFY` at every boot, and a deep-sleep wake is a boot (hence D14 and "a reset during the wait still rolls back"). `esp_ota_mark_app_invalid_rollback()` works on a VALID image and IDF already refuses an INVALID/ABORTED other slot, but not a NEW never-booted one, so the spec states the stricter rule and R5-fw-4 checks it. esp-mqtt runs the event handler before it sends the PUBACK, so a reboot inside delivery is redelivered forever (D8).
- **Patch split.** A (`spec/device-protocol.md`, six hunks), C (`spec/open-questions.md`, two sentences), D (`spec/flows.md`, one sentence): the owner, one `spec:` commit, any time; no spec-reading test turns red. No Patch B: no JSON example changes. Order-independent with the four pending patch sets (up/log A+B, R4-spec-1 A+C, up/telemetry A, the R4-be-1 prd telemetry row): A1 follows the `up/status` row, A2 sits directly before `### up/status` (not after the `artifact.sig` bullet), A6 after the "superseded layout." paragraph (not after `reset_reason`), A4/A5 between "…waits for the `apply` command." and `## Enrolment`.
- **Open for R5-fw-2.** An image the bootloader never armed (`TXN_BOOTED_NEW`) whose self-test fails has no `PENDING_VERIFY`: app-side `esp_ota_mark_app_invalid_rollback_and_reboot()` or `failed`, and which detail wins.
- **Named gaps.** The reason is lost when the board returns to a pre-R5 agent; a reset during a pre-apply `awaiting_safe_window` boots the staged image (the boot pointer moves at `staged`; true today); a predicate stuck at unsafe holds an unconfirmed image until a reset (visible, a power cycle recovers); after `cancel` the old image re-confirms once (`esp_ota_set_boot_partition()` writes NEW).
- **Rejected.** Reusing the stage's id for a verb; an `apply` without `cmd_id`; acks on `up/status`; `failed` for a cancel; "reboot while staged applies"; a `force` flag or a server timeout on `awaiting_safe_window`; pausing the deadline while unsafe; per-`power_class` confirm timeouts (supersedes `docs/HOBBYIST.md`'s "must differ by `power_class`"); a `dn/cfg` confirm timeout; a `selftest` key on `up/status`; a `safe_mode` status state; a recovery-partition safe mode (a new layout id); the vocabulary or `safe_mode` in the announce example.
- **Release gate.** R5 firmware tasks may build ahead of Patch A, unreleased (the R4-fw-2 precedent); the next agent/library release waits for the owner's Patch A.
- **Dry-run.** Patches A+C+D: the 24 spec-reading test files give 938 passed, 0 failed (same as unpatched); with all four pending patch sets before or after R5: 22 anchors each found once, the four patched spec files byte-identical across both orders, 938 passed on each; structural check `R5-SEMANTICS PATCH OK` (examples byte-identical, machine block plus `cancelled` only, `up/ack` row and section placed, verb block a plain fence, Library marker / Evolution rules / Open items unchanged).

---

## 2026-10-15 — The device log tail follows the strip's selected board, re-reads on that board's last_seen, renders the server's order with gaps below their line; T2 publishes up/log as a simulated board (R4-fe-2)

- **Built.** A `Device log` section after the fleet table (`DeviceLog.tsx`, `deviceLog.ts`), `api.deviceLogs`, `just log-e2e` (`frontend/scripts/device-log-e2e.mjs`). Frontend only: no spec, Python, migration or simulator change.
- **Decisions.** (1) Placement: its own section for the effective board, not inside the table (150 dev rows, a log is wide); fetch only for an id that is a fleet row, since an arrival would 404. (2) Hint: the selected board's `last_seen` string is the re-read trigger (an `up/log` batch moves it and `useFleet` already re-reads on the frame). (3) Gap below the line that carries `dropped`; `text: null` is a gap row only; adjacent gaps are not merged. (4) Level text is the raw level, `E`/`W` bold, `D`/`V` muted, open vocabulary; the label is in a `title`. (5) Always `limit=200`, and a muted note at exactly 200. (6) State is stored with its device id so a board switch reads as loading. (7) Text is React text in `<bdi>`.
- **Rejected.** A second EventSource or a log SSE event type (`sse_max_clients` slots, `fleet.ts` rule 1); reading the frame body; a per-row log in the table; client-side re-sort (the server's `logged_at DESC, id DESC` is the order); a simulator `--log-every` (R4-fw-3's half, owner-gated); paging (no server cursor).
- **T2 evidence.** `just log-e2e` against 8088: 8/8 pass, live line visible 1.1 s after the publish returned with no reload; `E2E_BREAK=tail-order` and `live-line` each exit 1. Details in `docs/features/health-telemetry.md`.
- **Gotchas.** The first CLI argument of the harness is the base URL, the second the out dir. A denied MQTT publish is silent, so setup checks the API after the first batch. `lines ?? []` matters: every existing Dashboard test answers the new URL with `{}`.

---

## 2026-10-15 — device.deploy frames carry cmd_id, state, is_terminal and a terminal detail; detail is the stored sanitised copy; v stays 1 (R4-be-2)

- **Built.** `DeviceEvent` gains `cmd_id`, `state`, `is_terminal`, `detail` (optional, after `reset_reason`). `ingestor/handlers.py` fills them from the written `deploy_events` row; `deploys.snapshot_detail` is now public (alias `_snapshot_detail` kept). Supersedes the R1-be-4 line "the envelope gains no new field". No frontend source, spec, migration or new event type.
- **Decisions.** (1) Four fields, not three: `is_terminal` is the server's answer, so a CI waiter never hardcodes the terminal vocabulary. (2) `device.deploy` only; null on every other type, a deduped replay and a status with no `cmd_id`. (3) `detail` only on a terminal row, and from the stored blob, never `status.detail` off the wire (the signed URL is a credential). (4) No `pct`. (5) No truncation of `cmd_id`/`state`: a hostile oversized value makes `emit` drop that frame with a WARNING, the row is still written. (6) `v` stays 1; skew is harmless both ways (old api forwards verbatim, new api defaults absent keys to null).
- **Rejected.** Emitting `device.deploy` from the API for `requested`/`publish_failed` (the POST answers synchronously); a replay/Last-Event-ID; sha256/versions in the frame.
- **T2 evidence.** Dev stack on 8088, `curl -N /v1/events` open across `just update-e2e`: 7/7 pass. Good cmd_id: 1 frame `state":"confirmed","is_terminal":true`, 7 frames `"is_terminal":false,"detail":null`. Broken cmd_id: 1 `rolled_back` terminal frame with the simulator's rollback text. 0 non-deploy frames carry a `cmd_id`; 0 occurrences of `http(s)://` on the stream; api healthy afterwards.
- **Gotchas.** Open the stream before the POST (no replay). The dev `.env` password is quoted, strip the quotes before using it in curl. Kill every `curl -N` before editing code, or the api's graceful reload waits on it.

## 2026-10-15 — The fleet table gains one Health cell: a boxed warning badge for fault resets (8 names), server counts and heartbeat health rendered as-is, no thresholds (R4-fe-1)

- **Built.** A `Health` column after Last seen (`FleetView.tsx::HealthCell`; logic in the new pure `deviceHealth.ts`, named so to avoid `health.ts`, the healthz poll): reset line, boot state, the server's fault count, heap low-water / uptime / rssi from the newest live heartbeat, and metrics (4 inline, `+N more`). `api.ts` mirrors `HealthSnapshot`, `FaultResets`, `MetricValue` and four OPTIONAL `DeviceSummary` fields before `deploy` (older API and fixtures keep working); `format.ts::formatBytes`; `.badge` in `index.css`. No backend change, no new hook or EventSource: the existing re-read already refreshes the cell live.
- **Badge rule.** `panic`, `int_wdt`, `task_wdt`, `wdt`, `brownout`, `cpu_lockup`, `pwr_glitch`, `efuse` get a boxed bold `! last reset: <raw reason>` (word + `.warn` weight + border, no tint: the theme is monochrome); every other name, including unknown ones, is muted text. Broader than the server's three counters on purpose: the badge is the current boot's cause, the counters are history. Not aged out client-side; the uptime beside it gives the age.
- **Rejected.** Separate columns per field (too wide for ~150 rows); reading `reset_reason` from the SSE frame (breaks `fleet.ts` rule 1); a client-side low-heap threshold (no spec number); hiding an old `panic` (it is the current boot's truth); a frontend test that reads `spec/` (the image build context has no `spec/`).
- **T2 evidence.** `just health-e2e` against the dev stack: `PASS panic-badge`, `PASS health-fields`, `PASS live-metric` (`sim_counter 3 -> 6`, no reload), `PASS traps`, `4/4 pass`; badge weight 700, solid border; control row `last reset: poweron`, no badge; `E2E_BREAK` on each scenario exits 1. API cross-check: `last_reset_reason` panic, `fault_resets.panic` 1, `min_free_heap` 98000, `boot_ok` true.
- **Gotchas.** (1) The dev frontend container runs Vite with `frontend/` bind-mounted, so a source edit is live at `localhost:8088` with no rebuild. (2) The harness locates rows by `data-device-id`: the dev fleet holds ~150 rows. (3) Each `just health-e2e` leaves two offline simulated boards in the dev DB.

## 2026-10-15 — Health, reset reason, telemetry and logs are stored: plain tables (no hypertable), faults derived on read with reconnect dedupe by hb uptime, logs bounded on write, telemetry bounded by the sweep; sweep in the ingestor (R4-be-1)

- **Built.** Migration `0009` (CRITICAL): seven nullable `devices` columns with no default (`last_reset_reason`, `health_at`, `uptime_s`, `rssi`, `free_heap`, `min_free_heap`, `boot_ok`) and four plain tables, FKs `ON DELETE CASCADE`: `device_faults`, `device_logs` (CHECK `dropped >= 0`), `device_telemetry`, `device_metrics`. One owner module per table (`faults.py`, `device_logs.py`, `telemetry_store.py`) and `retention.py` for every number plus `sweep`. Ingestor: `last_reset_reason` on every announce, live or retained, absent -> NULL (`store.py::_reset_values`, not in `ANNOUNCE_FIELDS`); the health group on every live hb (`_health_values`; `HeartbeatPayload` fields are tolerant, malformed -> NULL, `bool` never an int); a fault row on a live `panic`/`task_wdt`/`brownout` announce; telemetry and `up/log` batches stored. Every new write runs in a SAVEPOINT (`handlers._savepoint`) that logs a WARNING and keeps `last_seen` and the event. The ingestor runs `retention_loop` (sweep at start, always logged; hourly after, logged when it deleted something). API: `DeviceSummary` gains `last_reset_reason`, `fault_resets`, `health`, `metrics` (defaulted, before `deploy`, which stays last; PATCH carries them too); `GET /v1/devices/{id}/logs` and `/telemetry?name=` sub-resources (admin, 404 for unknown/decommissioned). `DeviceEvent.reset_reason` (on `device.announce` only, from the stored row; `v` stays 1). `config.fault_window_s` (86 400). `deploys.sanitise_device_text` (the old `_sanitise_detail`, now public and shared). `TELEMETRY_NAME_RE` moved to `telemetry_store.py` (the API validates `?name=` with it and may not import `fleetforge.ingestor`; `protocol.py` re-imports it). Simulator `--reset-reason NAME` (first boot only; an apply reboot still announces `sw`).
- **Plain tables, not hypertables.** Supersedes `design/production.md`'s "R4 telemetry is a hypertable and SPEC's 30-day retention is a Timescale retention policy" and the `db/models.py` lines saying so (both rewritten). Dev and the suite run `postgres:16` and `conftest.py` builds the test DB with Alembic: an unconditional `create_hypertable` fails there, a conditional one gives prod a schema no test runs against.
- **Faults derived on read, and the reconnect rule.** `GET /v1/devices` counts `device_faults` rows inside `fault_window_s`. The agent re-announces on every broker connect (S0-bug-3), so a live hb with `uptime_s` dates the boot, `floor = received_at - uptime_s - 1 s`, and every fault row of the device at or after the floor but the earliest is deleted. All times are receipt times, so latency only moves the floor later; an earlier boot's row is inside the window only if that boot died and rebooted within about a second of announcing, and even then the count is low by one, never zero. **Deviation from the plan:** the plan's floor had no `- 1 s`. On the dev stack the simulator's first announce left ~0.1 s after its uptime origin and the truncated `uptime_s` put the floor 80 ms after it, so the reconnect row was kept as "the earliest" and `panic` read 2. The 1 s is `uptime_s`'s resolution (`faults.UPTIME_RESOLUTION`); regression test `test_a_first_announce_inside_the_uptime_rounding_still_dates_the_boot`. Named gaps: reconnects in a boot that never heartbeats are counted; the sub-second crash-loop merge above.
- **Retention.** Logs: newest 5 000 per device trimmed on write in the SAVEPOINT (the `progress.py` idiom, via an `OFFSET :keep` cutoff on `(logged_at, id)`), 7 days by sweep and by the read predicate. Telemetry: 30 days and the newest 500 000 per device, both by the sweep (trimming that per batch costs far more than an hourly pass). Faults: 1 000 per device on write, 30 days by sweep. `device_metrics` swept at 30 days and hidden by the read. The sweep is in the ingestor because it is the single instance; the api has N workers. A board that never writes again still expires.
- **Rejected.** Hypertable, or a conditional one (above); stored fault counters (wrong the moment nothing decrements them); counting every live fault announce (one panic + two Wi-Fi blips = "3 panics"); hb fields as telemetry rows (~4 300 rows/device/day for a trend nobody asked for yet; named follow-up); `DISTINCT ON (device_id, name)` over `device_telemetry` for the latest metrics (no skip scan in PG16: every re-read walks every sample; hence `device_metrics`); the sweep in the api (N workers racing); new SSE event types (every frame already triggers the re-read; telemetry/log stay `device.seen`).
- **Spec status.** `spec/` untouched. Still owner-pending: up/log Patches A (payload) and B (5 000 lines), up/telemetry Patch A (R4-fw-2). **New proposal:** the telemetry per-device cap, a paste-ready `spec/prd.md` *Retention* row in `docs/features/health-telemetry.md` -> *R4-be-1 as built*; order-independent with up/log Patch B. `tests/test_device_health.py` reads the prd's numbers and, for the two per-device bounds, the proposals until applied.
- **Release notes.** Migration 0009; its downgrade drops stored health, faults, logs and telemetry (device-reported debugging data only). The api and ingestor ship together: a new ingestor on an unmigrated schema loses the announce and heartbeat UPDATEs (they write the new columns; the same skew as 0005/0006), while the new INSERTs alone would be SAVEPOINTed. R4-fe-1 must mirror the four new `DeviceSummary` fields into `frontend/src/api.ts` (before `deploy`).
- **T2 evidence (dev stack, `FF_HTTP_PORT=8088`).** `alembic current` 0008, 149 devices; `upgrade head` -> 0009, 7 columns and 4 tables present, still 149. Ingestor restart logged `retention sweep deleted logs=0 telemetry_expired=0 telemetry_capped=0 metrics=0 faults=0`. Simulator `--reset-reason panic --telemetry-every 2 --heartbeat-interval 5`: SSE `device.announce ... "reset_reason":"panic"`, heartbeats `"reset_reason":null`; `/v1/devices` row `last_reset_reason` panic, `fault_resets.panic` 1, `health.min_free_heap` 98000, `boot_ok` true, `uptime_s` 20 -> 30, `sim_counter` 13 -> 16, last key `deploy`; `/telemetry?name=sim_counter&limit=5` five samples newest first, 2.0 s apart. `docker compose restart mosquitto`: the board re-announced `panic` in the same boot (ingestor `announced reset_reason=panic` at 23:01:38 and 23:02:32); afterwards `fault_resets.panic` 1 and one `device_faults` row (with the 1 s fix; before it, 2, see above). `up/log` example plus a URL line via `just mqtt-pub` as the device: tail returned the `W` line first with `sensor: no reply <url>`, then the morse line 5.12 s earlier with `dropped: 3`; ingestor `log lines=2 dropped=3 unusable=0`, and `grep -c 'morse: HELLO'` / `'evil.example'` over the ingestor log = 0. A throwaway paho flood of 90 x 60 lines (max 2 892 B) left exactly 5 000 rows, newest `flood 89-59`. Inserted an 8-day log and a 31-day sample; ingestor restart logged `retention sweep deleted logs=1 telemetry_expired=1` and both were gone. With api+ingestor stopped and data in all four tables (5 000 logs, 263 samples): `downgrade 0008` removed the tables and columns, devices 151 (149 + the two boards enrolled for T2); `upgrade head` -> 0009, `/v1/devices` 200, ingest resumed. Test-only: the 500 000 telemetry cap, the fault trim and the sweep of faults/metrics.
- **Gotchas.** (1) The dev api hot-reloads on a source edit and its graceful shutdown waits for open SSE streams: close a `curl -N /v1/events` or the api sits "unhealthy". (2) `just sim` echoes its command line, enrollment token included (single-use, burned on enroll; still, do not paste the transcript). (3) Bulk SQL with a bound timestamp needs `CAST(:t AS timestamptz)` before `+ make_interval(...)` under asyncpg. (4) The `text` attribute on `DeviceLog` shadows `sqlalchemy.text` inside the class body, so it is declared last.

## 2026-10-15 — up/telemetry is proposed (owner: Patch A) and built unreleased: ff_telemetry()/Fleetforge.metric(), batched samples with age_ms, a 64-sample RAM ring; include/ gains a maker-API category (R4-fw-2)

- **Built.** Firmware: `ff_telemetry.c` (new) keeps the newest 64 samples (`name`, `value`, `esp_timer` time) in a static ring under a spinlock that guards struct copies only, and a flush task `ff_tel` (4096 B, prio 4, created on the first connect, never restarted) publishes `{"dropped":n,"samples":[{"age_ms","name","value"}]}` oldest first, QoS 0, retain 0, at most 900 B: back to back right after a connect, then at most one payload per 5 s. Publish, then commit: a failed publish keeps everything. JSON by hand (validated names need no escaping; exact size bound). `ff_mqtt.c` gains one topic (`topic_telemetry`, spelled with the others) and two calls: `ff_telemetry_session_up()` at the tail of `on_connected()` (after the announce, presence and `report_txn_on_connect()`) and `ff_telemetry_session_down()` in `MQTT_EVENT_DISCONNECTED`; nothing in the PUBLISHED/confirm branch, the confirm timer or `classify_txn` changed. Server: `clock.device_time(received_at, age_ms)`; `ingestor/protocol.py` `TelemetrySample`/`TelemetryPayload` (a bad sample is skipped on its own and counted in `unusable`); `handlers.py` logs one summary line per live batch from a registered device, after `touch_last_seen` (undecodable still moves `last_seen`), and drops a payload over 8192 B. No storage (R4-be-1), no event type, no migration. Simulator: `simulator/telemetry.py` (the firmware ring in Python, import-pure), `--telemetry-every SECONDS` (off by default) records `sim_counter` for the board's whole life. No version bump: `agent/version.txt`, `library.json`, `ff_lib_version.h` stay 0.5.0.
- **Names and why.** `esp_err_t ff_telemetry(const char *name, double value)` in the new public header `include/ff_telemetry.h` (matches the module and the topic, as `ff_logf` will match `up/log`); Arduino `bool Fleetforge.metric(const char *name, double value)` (what an Arduino user reaches for; one overload, since an `int` converts). Name rule `^[a-z][a-z0-9_]{0,30}$` (a Prometheus metric name); `ff_` reserved for the agent (refused on the device, accepted by the server). Callable before `begin()`, from any task, not from an ISR; never blocks, allocates or logs (R4-fw-3's forwarder must not recurse through it). Rejected: `ff_metric` (breaks the module/topic match), `ff_telemetry_record` (long for the one call a maker types), `Fleetforge.telemetry()` (reads as a getter).
- **Building ahead of the owner, and why.** The payload is a proposal (`docs/features/health-telemetry.md` -> *up/telemetry proposal*, Patch A: the Topics row and a new section before `dn/cmd`); `spec/` is untouched. Built in the same commit because (1) the TODO line scopes the build into this task (simulator and QEMU acceptance), unlike R4-fw-3, which is explicitly gated on its Patches A+B; (2) no spec file is touched and no spec-reading test turns red; (3) nothing is released, so the names freeze at the next agent/library release and Patch A is the gate before it (a rename now is cheap); (4) the S0-bug-3 rejection was about an announce field pinned by spec-example parity tests, while this channel is already in the spec and only its payload is new. `tests/test_telemetry.py` reads the spec's section once it exists and the proposal's until then, and pins that the two examples are byte-identical, so the owner's paste needs no follow-up commit.
- **The maker-API rule** (`tests/test_ota_component.py`, `MAKER_HEADERS`). `include/` was "exactly the headers `agent_main.c` includes, and every public function is one it calls". The stock agent has no application and no honest reason to call `ff_telemetry()`, so a second category exists: headers a firmware's own code calls. Rule: `agent_main.c` includes none of them and calls none of their functions, and both worked examples call every function they declare (`basic_idf/main/main.c` directly, the Arduino `Basic.ino` through `Fleetforge.cpp`). R4-fw-3 (`ff_logf`) and R5-fw-1 (`ff_selftest_fn`) reuse it. Rejected: calling `ff_telemetry()` from `agent_main.c` under a test `#if` (satisfies the regex, defeats the rule's intent); a stock-agent metric (invents product).
- **Limits.** 900 B device-side (spec text: under 2048 B), so the whole MQTT packet (33 B topic plus header) fits esp-mqtt's default 1024 B buffer without relying on its large-message path; about 15 short samples per payload, so a live session carries about 3 samples/s before `dropped` counts. 64 samples is about a 7-minute outage at the examples' one sample per 6.8 s.
- **Rejected.** Proposal only now and build later (the acceptance is a build acceptance; the gate on the names is the release); cJSON (size known only after a heap print); a FreeRTOS mutex (lazy creation races; a spinlock around copies suffices); drop-newest when full (the newest samples matter most after an outage); a server-side metric whitelist (names are the firmware's choice); a `seq` or a device timestamp on the wire (against *Clock*; `age_ms` suffices, as `up/log`); flushing from the heartbeat task (couples the cadence to `hb_s` and adds code to the CRITICAL `ff_mqtt.c`); QoS 1 (the spec table says 0, and the outbox would grow with the backlog).
- **Flash budget.** All four rebuilt (`BUNDLE OK`, 0 warnings) and raised to the exact measured bytes: esp32 +1,232 B, esp32s3 +1,248 B, esp32c3 +1,792 B, esp32c6 +1,824 B. RAM: the ring plus payload buffer are about 4.5 KB of BSS, and the idle task about 4.4 KB of heap, on every image, including the stock agent that never records a sample (its QEMU `free_heap` is about 9.7 KB below R4-fw-1's).
- **T2 evidence.** Local dev stack only. A watcher stamping every sample with the server's own decode and `clock.device_time`, and a judge (expected count, `dropped` 0, consecutive values, every gap within ±0.3 s of the period, earliest sample near the outage start) printed `TELEMETRY SPACING OK` for: (a) the sleepy simulator, three wakes of 6/7/6 samples 5.0 s apart; (b) the always_on simulator through a 40 s proxy cut, 31 samples drained in two back-to-back payloads (858 B, 705 B) 2.0 s apart; (c) QEMU, the Arduino `Basic` board through a 60 s MQTT cut (slirp closed the guest socket), 10 `morse_loops` samples 6.80-6.97 s apart in one 507 B payload, values continuing the live ones with no gap. The ingestor logged `device 000000000000 telemetry samples=10 dropped=0 unusable=0 oldest_age_ms=62890`. Details and verbatim payloads: `docs/features/health-telemetry.md` -> *up/telemetry as built*.
- **Gotchas.** (1) `test_arduino_package.py` compares the IDE package with **git-tracked** files: `git add` new component files before `just test`. (2) The simulator's QoS matrix test asserts every publish is QoS 1, which is why telemetry is opt-in there. (3) `FF_LOG_TAGS` in `Fleetforge.cpp` must list every component TAG (`ff-tel`). (4) `just sim` reads `FF_HTTP_PORT` from the environment, not `.env`: on this box run it with `FF_HTTP_PORT=8088` or the enroll goes to whatever owns 8080.

---

## 2026-10-15 — up/hb carries min_free_heap; boot_ok keeps meaning "not PENDING_VERIFY", read per heartbeat (R4-fw-1)

- **Built.** `ff_identity_heartbeat_json()` adds `min_free_heap` = `esp_get_minimum_free_heap_size()` between `free_heap` and `boot_ok`. One builder serves the agent, the ESP-IDF example and the Arduino library; no other firmware file changed. `proto` 1, agent/library version not bumped, nothing server-side (`HeartbeatPayload` ignores extra keys; storage is R4-be-1).
- **Semantics.** `boot_ok` = "the confirm path has run, or there was none to run": true unless otadata says `ESP_OTA_IMG_PENDING_VERIFY` (serially flashed true; OTA boot before the announce PUBACK false; after `confirm_this_image()` true; confirm timer fired false; `ESP_OTA_IMG_NEW` true). Body unchanged; the stale R0 comment was rewritten. Table: `docs/features/health-telemetry.md` -> *min_free_heap and boot_ok as built (R4-fw-1)*.
- **Simulator.** Sends `min_free_heap: 98000`; `StageRunner.boot_ok` is false only while the image it rebooted into is `pending` (not during the apply session itself, whose running image is the old, confirmed one), polled every beat by `_heartbeat_loop`.
- **Spec.** Patch B only (the hb example line); A (with A2's prose) and C stay the owner's.
- **Flash budget.** All four rebuilt; raised to the exact measured bytes (+128 / +112 / +112 / +96 B).
- **Rejected.** (1) `false` for `ESP_OTA_IMG_NEW`: an image OTA'd by a pre-0.4.0 agent onto a rollback-less bootloader would report `false` forever. (2) Deriving it from `ff_mqtt.c`'s `session_confirmed`: couples health to the CRITICAL confirm path and makes a serially flashed board "not ok" until its first PUBACK. (3) Caching at boot like `reset_reason`: a confirmed OTA boot would say `false` for its whole life.
- **T2 evidence.** QEMU esp32, Ethernet, `--hb 10`: `{"fw_version":"0.5.0","uptime_s":20,"rssi":null,"free_heap":227384,"min_free_heap":220764,"boot_ok":true}` (six hbs, all mechanically checked; ingestor decoded them). `boot_ok:false` from firmware is not observable in QEMU (an OTA reboot after the network panics before `app_main`); proven by the source-reading test and the simulator's `--confirm never` session.
- **Gotcha.** On this box the dev stack is on `FF_HTTP_PORT=8088` (8080 belongs to another container), so the runbook's `localhost:8080` / `10.0.2.2:8080` read as 8088; login uses `FF_ADMIN_PASSWORD` from `.env`.

---

## 2026-10-15 — The proto: 1 reservations are proposed, not applied: absent artifact.type means app, sub_versions, a presence lease, link_type dock, min_free_heap; the device_id widening is noted, not built (R4-spec-1)

- **Proposed** (all additive, Evolution rule 2, `proto` stays 1; no agent <= 0.5.x sends any of it, the server already ignores unknown fields). (1) `dn/cmd` `stage`: absent `artifact.type` means `app`; `mcu_fw`, `model_weights`, `fpga_bitstream` reserved. (2) `up/announce.sub_versions` (component -> version string), prose only: `TestAnnounceMatchesTheSpec` parses the announce example's keys and requires `ff_identity.c` to emit each, so the example is not touched. (3) `up/presence` optional `reason` and `expected_back_s`, a retained planned-absence lease. (4) `link_type` `dock` (the field is already an open vocabulary server-side). (5) `up/hb.min_free_heap` documented. Paste-ready text: `docs/features/health-telemetry.md` -> *proto: 1 reservations proposal (R4-spec-1, proposed)*. Nothing under `spec/`, `src/`, `agent/`, `tests/` or `alembic/` changed.
- **Hazards written into the spec text.** A deployed agent reads only `url`, `sha256`, `version` and `size` from `artifact` (`ff_mqtt.c`), so a `"type":"mcu_fw"` would be flashed as an app: a server sends a non-`app` type only to a device whose `capabilities` names a handler. The presence lease is a retained publish, and an unclean drop fires the LWT, whose bare `{"online":false}` replaces it: the device must close with a clean MQTT `DISCONNECT`.
- **Patch split.** A (device-protocol, four hunks) and C (open-questions, two sentences): the owner, one `spec:` commit, any time, no test turns red. B (the hb example gains `min_free_heap`): R4-FW-1's commit, with a test that parses the spec's hb example (today's `test_heartbeat_carries_the_spec_fields` uses a hard-coded key list). Order-independent with the pending `up/log` Patch A: the `min_free_heap` paragraph goes after the hb fence, not after "A sleepy device sends one per wake.".
- **Not built: the `device_id` widening** (`^[a-z0-9_-]{1,32}$`). It touches `identity.py::DEVICE_ID_RE`, the DB CHECK `devices.device_id_format` (a CRITICAL migration), the `mosquitto/acl` comment, topic parsing, three frontend files and the agent's `FF_DEVICE_ID_LEN`. Security gotcha: `device_id` is the MQTT username, and that shape admits the broker's service usernames, so an enroll could make dynsec provisioning act on a service client; a 12-hex id cannot collide. Any widening needs a reserved-name rule first. `spec/open-questions.md` already argues against widening in V1 (Patch C2 adds the collision).
- **Note for R4-FW-1.** `boot_ok` is already in the spec's hb example and already emitted by `ff_identity.c`; the task's "boot-success flag" is about its semantics, the new key is `min_free_heap`.
- **Rejected.** `sub_versions` in the announce example (reddens the parity test, nothing sends it); a `type` in today's stage payload (the server has one kind); reserving capability names for handlers that do not exist; a new `power_class` for "sortie" (it is presence, not power).
- **Dry-run.** Patches A+C: 318 passed, 0 failed (same as unpatched); A+B+C: 318 passed; the `up/log` Patch A before and after these patches both apply and give byte-identical spec files; structural check `R4-RESERVATIONS PATCH OK`.

---

## 2026-10-15 — The focus-group report: R4 and R5 are filed, groups, CI tokens and bench tooling are not

- **Input.** `~/Desktop/focus_group_consensus_report.md`, nine recommendations, 5/5 "AGREE" from five model-simulated personas. Synthetic, not customer evidence: it is a checklist of gaps, and unanimity on all nine carries no ranking. The PRD still targets about 5 boards and 25 devices.
- **Checked against the code** (all hold): `ff_mqtt.c` ignores `reboot`/`rollback`/`apply`/`cancel` ("not implemented by this agent"), parses and ignores `confirm_timeout_s`, and `confirm_timeout_cb` reboots with no safe-window check; `LIST_LIMIT = 200`; `examples/Basic` builds only `esp32` and `esp32s3`; `reset_reason` is logged, not stored (by design, S0-bug-3). One slip: it blames "Watch a board resets the board" on S0-bug-3, which was the investigation; the EN pulse is S0-fe-5.
- **Against the roadmap.**
  - Items 1-2 (health, `reset_reason` persistence, `up/log`) are **R4** as already planned, plus `min_free_heap`, the deploy-event enrichment and the `age_ms` batching. Filed: R4-fw-1..4, R4-be-1..2, R4-fe-1..2, R4-test-1.
  - Items 3-4 (self-test, command seam, safe window, sleepy helpers) are **R5** as planned, plus the verbs, the safe-window gate and `confirm_timeout_s`, which the roadmap does not name. Filed: R5-spec-1, fw-1..7, be-1..2, fe-1, test-1. The safe-window gate on the rollback reboot is the one with a safety argument (SWARM.md section 2); that is why R5 leads with it.
  - Item 5 (signing, `Range` resume) is **R6** as planned. The one amendment worth taking: a detached signature verified against a device keyring, so the server never holds a production private key. Not filed: R6 is not active; amend `R6-SEC-1/2` when it opens.
  - Item 9 (additive `proto: 1` reservations) is not in any release. Filed as **R4-spec-1**, a proposal only; the `device_id` format widening is a server schema change and stays unbuilt.
  - Items 6 (machine tokens, provenance, CI CLI), 7 (flash-bench CLI, registry publishing, Wi-Fi rotation, `ca` in `ff_cfg`) and 8 (groups CRUD, ring rollouts, list pagination) **contradict the stated plan**: groups and bulk deploy are V3 (`spec/prd.md`, `groups-deploy.md`: "five heterogeneous boards have nothing to bulk-deploy"), provenance and push ingestion are R7/R8. Deferred until there is a second operator or a homogeneous fleet. The `ca` in `ff_cfg` and "watch serial without reset" are the two cheap pieces; revisit them with the V3 gateway work and the console.
- **Rejected.** Pulling ring rollouts forward as "late V1": it needs `groups` and `max_parallel_apply`, which only matter at fleet size.

---

## 2026-10-15 — reset_reason is built: the agent announces esp_reset_reason() once per boot, the ingestor logs it on a live announce and stores nothing; sw and panic proven in QEMU only through reboots that precede the network (S0-fw-6)

- **Built.** `ff_identity.c` reads `esp_reset_reason()` once in `ff_identity_init()` (all three boot paths call it, so agent, ESP-IDF example and Arduino library stay identical with no new call), maps the 15 IDF 5.5 names lowercased, anything else `unknown`, and emits `reset_reason` after `capabilities`, before `lib_marker`. Patch B applied to the spec example in this commit (Patch A: owner, 74b6bcb). `proto` 1, agent version untouched. Simulator: `poweron` first, `sw` after the apply reboot and the confirm-timer rollback. Ingestor: `AnnouncePayload.reset_reason` via `announce_fields.normalize_reset_reason`; logged on a live announce only; no column, migration or API field. `EnrollRequest` (`extra="ignore"`) verified by test.
- **Malformed rule, one addition to the proposal.** Not a string or > 16 chars -> null (as proposed); a control character or lone surrogate -> null too, because this is the one announced value whose content is written into the server log, and a board must not be able to write a line break there. An unknown name is still logged as-is.
- **T2 evidence (QEMU, local stack).** `poweron` from `just agent-qemu esp32 --fresh`, on the wire and in the ingestor log. `panic` on the boot after a `FF_FAULT_TEST=bootloop` image's `abort()` (bootloader rollback to this tree's 0.5.0). `sw` on the boot after the confirm timer's `esp_restart()` in a `FF_FAULT_TEST=hang` image (rollback reboot), retained announce `"reset_reason":"sw"`. Details: `docs/features/health-telemetry.md` -> *reset_reason as built (S0-fw-6)*.
- **Not proven, and why.** The literal criteria, `sw` after the *apply* reboot and `panic` after an `abort()` *after connect*, cannot run here: any software reset after the network has run panics in `esp_timer_impl_init` before `app_main` on the next boot (the known harness limit), so nothing is announced. Reproduced with the apply reboot and with a throwaway after-connect `abort()` build. The two substitutes exercise the same `esp_reset_reason()` read and the same announce on unmodified code; the trigger point differs. New harness finding, root cause still open: the panic is `timer_alarm_isr` running inside `esp_intr_alloc` before `s_alarm_handler` is set, with timer group 0's LACT interrupt still raised after the soft reset; quiescing LACT, the openeth NIC or CPU `INTENABLE` at core init did not cure it.
- **Flash budget.** All four targets rebuilt; `APP_SIZE_BUDGET_BYTES` raised to the exact measured bytes (+384 to +480 B each).
- **Rejected.** (1) Committing a QEMU-only quiesce shim: it did not work, and a startup hook in the shipped component is a CRITICAL-path change for a harness. (2) A new `ff_*` call in each boot path for the read: `ff_identity_init()` already runs once per boot in all three, and a new call would be one more thing the parity tests must hold together. (3) Storing it: R4.

---

## 2026-10-15 — The object store is built once per process, not once per request (S0-bug-4)

- **Symptom.** `monitor-resources.sh` warned every 10 minutes ("containers exceeding 80% CPU or 80% memory"); load was 0.13 on 2 vCPUs, so it was memory: `prod-fleetforge-api-16` at 214 MiB of 256 MiB (84%), up from 75 MiB. It stepped at 21:40-21:50 UTC and 22:00-22:10 on 2026-10-14, the bench session (login, catalog load, enrol, upload), and never came back.
- **Cause.** `get_object_store` called `create_object_store(settings)` on every request that took `ObjectStoreDep`. `storage/factory.py` says clients are built once per store, but the cache dict lives inside the store, and the store was new each request. Each request therefore built a boto3 `Session` and client. Measured on the v0.6.0 image: 30 reads through one store +12 MiB, 30 reads with a new store each +133 MiB (about 4.4 MiB per request), and `gc.collect()` returned only 24 MiB. A 1 MB agent-part download repeated 15 times peaked at 243 MiB. `MALLOC_ARENA_MAX=2` helped only a little (225 MiB). Uploads were not the problem (about +1 MiB each). Argon2 login costs a one-off ~20 MiB, and the first store use ~23 MiB; both are expected and flat.
- **Fix.** `api/deps.py` keeps one store in a module-level cache, keyed on the 15 settings the factory reads (`_STORE_SETTINGS`), under a lock. A failed build raises the same 503 and is never cached. `get_object_store` stays a plain dependency, so `dependency_overrides` still work. After: 50 downloads 119 to 124 MiB.
- **GCS is unmeasured.** The profile used MinIO (S3). On prod (GCS, impersonation) each request also resolved ADC and minted a token, so the cost there is probably higher and every request paid a token call; not measured, to avoid reading prod's bucket. Check prod RSS after the deploy.
- **Gotcha for later.** A new store setting must be added to `_STORE_SETTINGS`, or a changed value keeps the old store. The CLI paths (`firmware`, `storage`, `deploycheck`) still call `create_object_store` once per process, which is fine.
- **Not done.** The api memory limit (256M in `services/prod/docker-compose.yml`) is unchanged; whether to raise it is the owner's call. The alert message still says "CPU or memory" and cannot tell which tripped.

---

## 2026-10-14 — Firmware logs are R4, not Sprint 0; the up/log payload (batched lines with age_ms) and a 5 000-line-per-device bound are proposed, not applied (S0-fw-5)

- **Decided: R4.** (1) Sprint 0 is bricking, broker auth and security; a missing remote log is none of these (the board updates, confirms and rolls back without it, and "Watch a board" still shows every line over serial). (2) Storage needs a new table and an Alembic migration (CRITICAL) plus retention, which is R4-BE-1's existing scope (7d logs). (3) R4 is the next release. (4) R0 shipped without `up/log` and nothing broke: the ingestor already only moves `last_seen` for it. (5) Filing the proposal now settles the payload before R4-FW-2 designs `up/telemetry`, so both share `age_ms` (SWARM 5.1). Counter-point: "Watch a board" pulses EN and reboots the board (S0-bug-3), so a running board's log cannot be read without a restart. Real friction, not Sprint 0 class; the owner can pull R4-FW-3 forward if it hurts.
- **The proposal.** `up/log`, QoS 0, not retained; one payload is a batch `{"dropped", "lines":[{"age_ms","level","text"}]}`, oldest first; sent only by an explicit library call; `text` <= 200 B, payload < 2048 B, one publish per 5 s; `logged_at = received_at - age_ms` (no device wall clock); retention 7 days and the newest 5 000 lines per device. `proto` stays 1. Paste-ready Patch A (`spec/device-protocol.md`) and Patch B (`spec/prd.md`): `docs/features/health-telemetry.md` -> *up/log proposal (S0-fw-5, proposed)*. The owner applies both in one `spec:` commit; nothing in code depends on it yet.
- **Build tasks** (rows in the health-telemetry R4 table, not TODO.md): R4-FW-3 (new; agent + library, touches CRITICAL `Fleetforge.cpp`), R4-BE-1 (existing; note added for its `up/log` half), R4-FE-2 (new; log tail). Blocked on the owner applying Patches A+B.
- **Rejected.** (1) Build in S0 now: edits the near-frozen protocol before acceptance and needs a migration R4-BE-1 owns. (2) Forward all ESP_LOG output: misses `printf`/`Serial.printf` (the `morse:` line), recurses through esp-mqtt/mbedTLS logging, floods a QoS-0 channel. (3) Tee the UART/USB-CDC stream: intrusive, takes over the sketch's port. (4) A server-side log level: not in v1. (5) A per-batch `seq`: costs bytes, `dropped` is enough. (6) Device wall-clock timestamps: against *Clock*. (7) Keeping a dead board's lines beyond 7 days: a retention change the prd does not promise; a named gap.
- **Dry-run.** Patches A+B in a throwaway worktree: 253 passed, 0 failed (same as unpatched); structural check `UP-LOG PATCH OK`.

---

## 2026-10-14 — The two unexplained announces were a reboot (cause unknown, probably the operator) and an MQTT reconnect; reset_reason is proposed, not built (S0-bug-3)

- **Event 1, 22:23:22: reboot.** `device_progress` (verbatim, UTC): `22:23:12.65 link_up|wifi`, `22:23:19.50 time_synced|`, `22:23:22.77 mqtt_connected|`. `link_up` and `time_synced` are reported once per boot; `mqtt_connected` on every connect. Heartbeat phase moved `:19` -> `:22` and size fell to 81 B at 22:22:19 and 22:24:22 (82 B otherwise; `uptime_s` back to two digits); the hb due at 22:23:19 is missing. Broker: `1792016601: Client 84c7bbe0e8c0 already connected, closing old connection.` (= 22:23:21 UTC), so the board returned before the 45 s keepalive expiry and no LWT fired. No `fleetforge-cmd-*` connect in the 60 s before (commander lines are at 1792016450, 1792016988, 1792018368, before the real deploys) and `deploy_events` has zero rows 22:21:30-22:52:00. No `up/status` followed, so no OTA transaction was open: not a rollback (deploy `7554ff73...` ended `confirmed` at 22:21:19.92).
- **Event 2, 22:49:26: reconnect inside the boot, not a reboot.** `device_progress` holds `22:49:26.34 mqtt_connected|` only (no `link_up`, no `time_synced`). hb phase stays `:20` (22:48:20 -> 22:50:20), size stays 83 B (`uptime_s` continuous since the 22:30:20 OTA boot). Two hbs arrive with the announce at 22:49:26: queued in the esp-mqtt outbox while the socket was down. Broker: `1792018164: Client 84c7bbe0e8c0 already connected, closing old connection.` (= 22:49:24). The announce is republished on every broker connect by design. The socket-loss cause (Wi-Fi blip, NAT, TCP stall) is not recorded and is harmless.
- **How to tell them apart next time.** A `link_up` + `time_synced` pair within ~15 s before `mqtt_connected` is a boot; `mqtt_connected` alone is a reconnect. The task's premise was half wrong: "nothing the board sends tells the two apart" holds for the reset cause only. `up/hb.uptime_s` would also separate them, but the server does not store it (R4). `device_progress` is trimmed per device (`progress.py::TRIM_SQL`), hence the verbatim copy above.
- **Probable cause of event 1 (hypothesis).** The operator was bench-testing a custom Morse build on this C3 (c71ee8d). The dashboard console pulses EN on every "Watch a board" (S0-fe-5, `boardConsole.ts`), which yields exactly this signature; a manual reset or re-plug looks the same. Not excluded: a panic or watchdog reset in the custom image, a brownout. **Operator question owed:** "At about 22:23 UTC on 2026-10-14, did you press reset, re-plug, or attach a serial console ('Watch a board' in the dashboard, or another monitor) to `84c7bbe0e8c0`?" A yes closes event 1.
- **Correction.** The S0-bug-2 plan called 22:49:26 a "fast reboot". It was a reconnect. The S0-bug-2 entry above does not say so; nothing else needs correcting.
- **Decided: `reset_reason` is a proposal only.** No agent, simulator, ingestor or spec change. Patches A/B are in `docs/features/health-telemetry.md` -> *Reset reason proposal*. The owner applies Patch A in a `spec:` commit; Patch B goes only in S0-fw-6's commit (it turns `TestAnnounceMatchesTheSpec::test_the_firmware_builds_exactly_the_spec_keys` red until `ff_identity.c` emits the key). Dry-run: A green, A+B fails exactly that one test. S0-fw-6 is blocked on the owner.
- **Rejected.** (1) Building the field now: edits the near-frozen protocol without owner acceptance, against the CRITICAL.md row and the lib_marker precedent. (2) Storing `uptime_s` or reset reason in the DB now: a migration (CRITICAL) for R4 telemetry, and `device_progress` already separates reboot from reconnect. (3) Making the console's EN pulse report itself: the console has no server channel; the reset reason covers it once built.

---

## 2026-10-14 — A live announce reports online:null, never false, until its presence lands (S0-bug-2)

- **Root cause.** The agent publishes retained `up/announce` and then retained `up/presence {"online":true}` on every connect. At announce time `presence_reported` still holds the previous session's value, so `is_online` was False for an `always_on` board and the event said `online=False`. Prod `84c7bbe0e8c0`: 21:47:37 (after re-enroll reset presence to NULL) and 22:14:50 (after an LWT at 22:09:59).
- **No dashboard flicker was possible.** The browser never reads the event's `online` (`fleet.ts` rule 1; 2026-09-10): a frame only means "re-read `GET /v1/devices`". Only the SSE snapshot (`curl` watchers) and the ingestor log carried the wrong value.
- **Rule.** A live (`retained=False`) announce reports `true` if `is_online(row)` is true, else `null`. A retained announce replay reports `is_online(row)` (a dead board's replay still reads `false`). Every other message type is unchanged and always a bool. `presence.py` is unchanged; `handlers._online_snapshot` only withholds its answer.
- **Envelope.** `DeviceEvent.online` becomes `bool | None`, still required. `v` stays 1: the consumers are the browser (reads `type` only), `api/eventstream._on_notify` (same model, same image) and human `curl` watchers; no `spec/` section defines the envelope. Deploy skew: an old api validating a new ingestor's `null` drops that frame ("dropping an unparseable ff_events payload"); api and ingestor ship and restart together and the presence frame that follows is a bool, so this is harmless.
- **Rejected.** (1) Report `true` on a live announce: asserts presence beyond `is_online`, and lies if the presence publish is denied. (2) Write `presence_reported = true` on announce: takes presence authority from `up/presence`/LWT (`spec/device-protocol.md`) and breaks `test_announce_says_nothing_about_presence`. (3) Reorder the agent: a firmware change across a deployed fleet, and the order is load-bearing on the board.

---

## 2026-10-09 — R3 is prepared as v0.5.0: agent and library 0.5.0, a library README that routes to the two example READMEs, the pinned git lib_deps form proved with git+file; nothing published or deployed (R3-rel-1)

- **D1. Versions.** App `0.4.3 -> 0.5.0` (minor), agent and library `0.4.7 -> 0.5.0`: the component's public surface ships for the first time (additive-only from now on) and the announce gained `lib_marker`. The app tag equals the library version this once, so `#v0.5.0` installs `Fleetforge@0.5.0`. A coincidence, not a policy; the two numbers stay independent.
- **D2. The library README** is `agent/components/fleetforge/README.md`. It routes to the example READMEs for every command and holds no `# quickstart:` block, so the scripted blocks and their names stay unique.
- **D3. The pinned git form** `lib_deps = Fleetforge=https://github.com/tvaroska/fleetforge.git#v0.5.0` is documented in the library README and Basic's *Your own project*. It is true only after the owner pushes `main` and the tag; proved locally with `git+file://`.
- **D4. Two commits and a local tag.** `R3-rel-1: ...` (agent bump, READMEs, docs, test), then `chore: release v0.5.0` (the five app version files), tagged `v0.5.0`, annotated, not pushed.
- **D5. The Arduino release zip** is built locally (`just lib-arduino-package`, gitignored); attaching it to a GitHub release is the owner's step.
- **Release notes and owner checklist:** `docs/features/ota-library.md` -> *R3-rel-1*.
- **Supersedes.** The R3-fw-7 "Deferred" git-URL form (now documented), and the R3-fw-6 named gap "Dev-built 0.4.7 agents announce lib_marker" (now unambiguous: 0.5.0 and later).

---

## 2026-10-09 — CUJ-1's T3 gate passes on the library path: one full lib-quickstart grades steps 1-2, 3, 5, 6 on the device, update-e2e the dashboard half and the wrong-layout refusal; the duplicated-command assertion and the 2 s criterion are listed, not scored (R3-test-2)

- **Run order.** lib-quickstart needs the api on 10.0.2.2 origins and update-e2e needs localhost origins, so they ran sequentially with the api restored in between. Both passed (1833 s; 7/7).
- **Grading rules.** `fw_version` is graded only from lib-quickstart (simulated boards report what they are told). "Read from the running image" is the `morse: HELLO (firmware X)` line plus the API row, with identical X. Trap mapping as in the plan; device halves of wrong layout are a procedure.
- **jeep.** No hard-fail trap; PASS on every assessable item; the duplicated deploy command was NOT ASSESSABLE.
- **Named gaps.** Duplicated deploy command (no harness); dashboard within 2 s (no harness).
- **Suggested follow-ups, not filed.** A dedupe re-publish in `phase_ota`; a 2 s measurement. Details: `docs/features/ota-library.md` -> R3-test-2.

---

## 2026-10-09 — The dashboard lists partition profiles and adopts a detected one by naming it; the upload form offers only adopted profile names; profiles are re-read on device.announce via a counter on useFleet (R3-fe-1)

**Decided.** A "Partition profiles" section (`PartitionProfiles.tsx`) sits between the fleet
table and the upload form. Naming a detected map is what adopts it (R3-be-2 D3). Details, the
e2e scenario and the T2 table: `docs/features/board-profiles.md` → *R3-fe-1*.

- **D1. The hint.** `useFleet` already parses each frame's `type`; it gains `announceSeq`,
  bumped once per coalesced burst that held a `device.announce` frame and on every stream
  re-open after the first (a re-open is the resync path: frames in the gap are lost; the first
  open is skipped because the profile hook reads on mount). No second `EventSource`, no new
  event type, no poll. Nothing is rendered from the frame body (rule 1 of `fleet.ts`).
- **D2. `useProfiles` is in `profiles.ts`, the `deploy.ts` idiom**: a non-rendering hook that
  reads on mount, when `announceSeq` changes and after an adopt, drops out-of-order responses,
  sends a 401 to the login gate, and turns any other failure into a string under the heading.
  Dashboard owns the one instance and passes it down; `UploadBuild` still never fetches on mount.
- **D3. Upload options are adopted names only** (every deployable `layout_id`, sorted).
  `d.partition_layout` is no longer an option source, so `unknown` can never be offered. The
  default is the one effective layout every board of the chosen chip shares (the server's D7
  order: announced adopted id, else the adopted profile with the board's fingerprint), else
  `ab-4m-v1`. Before the list loads, or if it fails, the only option is `ab-4m-v1`; the
  server's 400 stays the authority.
- **D4. Naming is a client check that mirrors the server, then a PATCH** (`profileName.ts`, the
  `boardName.ts` posture; the server is authoritative). The body is `{layout_id}`;
  `ota_slot_size` is sent only when the row has no measured slot. Duplicates are the server's 409.
- **D5. The proof is a 7th `update-e2e` scenario, `adopt-detected-profile`**, not a new script
  (CUJ-1's Driver names scenarios by id, so this is additive and no `spec/` text changes). Each
  run uses a per-run fingerprint and name, so a run leaves one adopted profile behind; a failure
  before naming deletes its own pending row so failed runs cannot fill the cap of 32.
- **Named gaps.** No create-by-fingerprint UI and no forget-pending UI (the API has both). The
  pre-check card's refusal says "adopt it by naming it" but does not link to the section. The
  board-profiles spec paragraph proposed by R3-be-2 is still unapplied; this task does not need it.

---

## 2026-10-09 — An upload stores has_lib_marker (BOOLEAN NULL, migration 0008); no_library_marker is the second gating code, raised only when the verdict is false; the device's announced lib_marker stays unstored (R3-be-1)

**Decided.** `POST /v1/artifact` reads every accepted upload with `fleetforge/lib_marker.py`
and stores the verdict; the deploy pre-check gates a deploy of an unmarked build as
`no_library_marker` (`needs_override: true`) and never reads bytes. Details and the T2
table: `docs/features/ota-library.md` → *R3-be-1*.

- **D1. Storage: `artifacts.has_lib_marker BOOLEAN NULL`**, migration `0008`, no default, no
  CHECK. Tri-state, the `devices.rollback_capable` idiom: NULL = never scanned (every row
  from before R3-be-1; never warns), `false` = scanned, no valid marker (gates), `true` =
  marker found. Not `provenance` (producer-copied, "never recomputed"). Not the format
  integer with a 0 sentinel: the gate needs only presence; `format` and `lib_version` go to
  the upload log line. **Rejected:** storing `lib_version` (no consumer yet; additive later).
- **D2. The reader is `src/fleetforge/lib_marker.py`** (pure, stdlib, never raises), the
  spec's reading rule verbatim. `MAGIC` is pinned equal to the spec table and to
  `ff_marker.c` by `tests/test_lib_marker_reader.py`.
- **D3. Scan every accepted upload**, after the merged-image refusal and before the blob
  write. The verdict is always a bool from R3-be-1 on; a missing or malformed marker never
  refuses an upload.
- **D4. A re-upload backfills a NULL verdict**: `ON CONFLICT (sha256) DO UPDATE SET
  has_lib_marker = EXCLUDED.has_lib_marker WHERE artifacts.has_lib_marker IS NULL`. The
  verdict is a pure function of the bytes the digest names, so filling it is idempotent and
  never changes a known verdict; every other column keeps its first writer's value. This is
  the operator's route to a verdict for a pre-R3-be-1 artifact; the migration cannot
  backfill (no object store).
- **D5. The gate** is `gating_warnings(device, artifact=…)`: `rollback_incapable` (the
  board's) first, then `no_library_marker` (the image's) when `artifact is not None and
  artifact.has_lib_marker is False`. `GATING_CODES = (ROLLBACK_INCAPABLE,
  NO_LIBRARY_MARKER)`. `artifact` is a required keyword and `ResolvedArtifact.has_lib_marker`
  has no default (the R3-be-2 `catalog` idiom), so no path silently drops the gate.
- **D6. The device's announced `lib_marker` is not stored.** Nothing reads it and the spec
  forbids warning on it; the ingestor and `/v1/enroll` keep ignoring the key. It becomes a
  column when a dashboard reads it. Supersedes the parenthetical "storing it … are R3-be-1"
  in the R3-fw-6 entry below.
- **Named gaps.** Artifacts uploaded before R3-be-1 have no verdict and never warn until
  re-uploaded (D4); the marker proves the library is linked, not started (spec); the
  downgrade drops every stored verdict (a re-upload recomputes it, and NULL is fail-open).
  No `spec/` change is proposed: the spec already says all of this.

---

## 2026-10-09 — The library marker is ff_marker.c's ff_lib_marker, its lib_version is FF_LIB_VERSION in every build, read by the announce (lib_marker, last key) and the ff-id image line; Patch B applied (R3-fw-6)

**Decided.** The marker R3-spec-3 specified is one `const ff_lib_marker_t ff_lib_marker`
(64 B, `aligned(4)`) alone in the component's `src/ff_marker.c`, declared in the private
`src/ff_marker.h`. Nothing force-keeps it. `ff_identity.c` keeps it linked by reading it:
the announce adds `"lib_marker": ff_lib_marker.format` as its **last** key, and
`ff_identity_init` logs `ff-id: image: fleetforge library <lib_version>, lib_marker <format>`.
Spec Patch B (the key in the `up/announce` example) is in the same commit, as approved.
Details and the T2 table: `docs/features/ota-library.md` → *R3-fw-6*.

- **The version mechanism (settles R3-fw-2's "how the number reaches ff_marker.c").**
  `lib_version` is `FF_LIB_VERSION` from `src/ff_lib_version.h` in EVERY build: the IDF
  agent, a maker's own ESP-IDF project and Arduino. That header is already pinned equal to
  `agent/version.txt` and `library.json` (`tests/test_arduino_library.py`), so there is still
  one number in three files. **Rejected:** `PROJECT_VER` (in a maker's IDF project it is the
  maker's version; `basic_idf` sets `1.0.0`, and T2-D shows the marker says `0.4.7`), and
  CMake reading `../../version.txt` (a copied component has no such file, and the Arduino
  builds have no CMake).
- **Why the boot line sits before `device_id`.** `frontend/src/diagnostics.ts` takes the
  device id from the last `ff-id` line's first 12-hex run. The `image:` line has no such
  run and goes before `device_id %s`, so nothing new lands after the `device_id` line.
  It is also the address escape: `lib_version` passed as a `%s` argument.
- **Why nothing force-keeps it.** A `used`/`retain` attribute, a linker `KEEP()` or `-u`
  would put the marker in a sketch that has the library installed but never calls it, which
  the spec forbids. T2-E proves gc-sections drops it there (`ff_marker.c.o` compiled, image
  unmarked). A test bans those spellings in the component and the package script.
- **Size.** +192 B (esp32), +224 B (esp32s3, esp32c3, esp32c6), budgets raised to the exact
  measured bytes.
- **T2.** The released 0.4.7 source (`v0.4.3`) never mentions the key or the magic; the
  pre-rebuild bundles and `tests/fixtures/firmware/*.bin` have no marker. Every library
  build from this commit has exactly one valid marker, `format=1 lib_version=0.4.7`: agent
  ×4, Arduino Basic esp32/esp32s3, the QEMU bundle, `basic_idf`. The QEMU example and the
  stock agent in QEMU announce `lib_marker: 1` as the last key, enroll 200, and are online
  (not refused).
- **Named gaps.** LTO is unmeasured (no build uses `-flto`). The marker proves the library
  is linked, not started. Dev-built 0.4.7 agents from this commit announce `lib_marker`;
  "agents ≤ 0.4.7 announce none" means the released 0.4.7 until the R3-rel-1 bump. The
  ingestor and enroll ignore the key (storing it and `no_library_marker` are R3-be-1). The
  Arduino IDE package check (`just lib-arduino-check`) was not re-run.

---

## 2026-10-09 — Spec proposals R3-spec-3 and R3-spec-2 accepted and applied; S0-bug-1 and R2b-test-1 withdrawn

**Decided (owner): apply Patch A of the library-marker proposal (`device-protocol.md` *Library marker* section and `lib_marker` prose, `flows.md` line 144, `open-questions.md`) and Patch A (a)-(d) of the CUJ-1 Driver proposal (`cujs.md`), plus `rolled-back` → `rolled_back` in `standards.md`. Patch B (`"lib_marker": 1` in the announce example) goes only in `R3-fw-6`'s commit. S0-bug-1 and R2b-test-1 are withdrawn: both need a person at a board, which the 2026-10-08 decision dropped.** Unblocks `R3-fw-6`, `R3-be-1`, `R3-test-2`. The release step stops before any prod deploy or `just agent-publish-all`.

---

## 2026-10-09 — Partition profiles are a global table keyed by fingerprint; builtins are seeded by migration and immutable; an unknown table is recorded as a pending `detected` profile and adopted by naming it; the gate resolves an `unknown` board by fingerprint (R3-be-2)

**Decided.** Migration `0007` adds `partition_profiles`; `fleetforge/partition_profiles.py`
owns it; `/v1/partition-profiles` (admin) lists, creates, adopts and deletes. Details, the
API table and the T2 table: `docs/features/board-profiles.md` → *R3-be-2*.

- **D1. Global, not per fleet or per group** (settles the board-profiles.md open item).
  One install is one fleet in v1 and in V2 self-hosting; `device_groups` scope enrollment
  tokens, not fleets. A flash map is a physical property of a board, so moving a board
  between groups must not change what it can take. Artifacts are global (`artifact_versions`
  is keyed by `(target, version)`) and a profile id is what an artifact's `partition_layout`
  names, so the two scopes must match. V3 multi-tenant adds a tenant column to both, additively.
- **D2. PK `partition_table_sha256`; `layout_id` a nullable UNIQUE name.** Every board on an
  unsupported map announces the same `unknown` (the R3-fw-5 named gap), so the id cannot be
  the key. Every row has a fingerprint (NOT NULL): a profile without one cannot be checked.
- **D3. Origins `builtin | user | detected`**, provenance, never changed after insert.
  Adopted (`adopted_at IS NOT NULL`) = named = deployable. Builtin and user rows are adopted
  at insert; only `detected` may be pending. All of it is DB CHECKs.
- **D4. Builtins are seeded by the migration (retyped literals, no `fleetforge` import) and
  immutable**: PATCH/DELETE answer 409. A new builtin is a new migration INSERT (check first
  that no user row holds that name or fingerprint). Tests pin DB builtins = `BUILTIN_LAYOUTS`
  = the spec table. An adopted name never changes either (artifacts carry it as text); such
  a profile is deletable only while no artifact names it. A pending row is always deletable.
- **D5. `SUPPORTED_LAYOUTS` → `BUILTIN_LAYOUTS`.** It is the seed, the firmware's
  `FF_KNOWN_LAYOUTS`, the spec table and what agent bundles validate against (bundles are
  built only from `agent/partitions.csv` and load with no DB session), not "what this server
  supports". The runtime set is a `LayoutCatalog` loaded from the table per request
  (`load_layout_catalog`, not `load_catalog`, which is the agent catalog). Old DECISIONS
  mentions and `agent/` comments are left as they are (append-only; firmware is CRITICAL and
  an edit marks bundles stale): R3-fw-6 fixes the `ff_identity.c` comment.
- **D6. Detection is in the ingestor**, after the device UPDATE returned a live row, as one
  `INSERT … SELECT … WHERE NOT EXISTS (that id is a profile) AND pending < 32 ON CONFLICT
  DO NOTHING`, in a SAVEPOINT. Idempotent under QoS-1 redelivery and the retained replay,
  capped (`DETECTED_PENDING_CAP`, WARNING when it blocks), never for an unregistered or
  decommissioned device, never in `store.py` (its "never INSERT" is about `devices`). A
  failure (`except SQLAlchemyError`, never bare) is logged and the announce stands. Enroll
  does not detect: the first announce follows within seconds. A slot ≤ 0 is stored NULL.
- **D7. The gate resolves the effective layout**: the announced id if it is adopted (with the
  existing fingerprint check, so a builtin id on another table is still
  `partition_table_mismatch` and creates no row); else the adopted profile the fingerprint
  matches (the only way an `unknown` board is ever deployable); else `unsupported_layout`,
  whose old sentence is a byte-for-byte prefix plus one adoption sentence when the
  fingerprint is pending. `catalog` is a required keyword of `refusals`, so
  `deploy_precheck.py` stays pure and no path falls back to builtins silently. The
  pre-check reports `device_partition_profile` (additive).
- **D8. `MAX_PROFILE_SLOT_SIZE = 4 MiB`** for operator-entered or adopted slots: nginx's
  `client_max_body_size 4m` on `location = /v1/artifact`; a larger slot would let the API
  accept what nginx answers with its own HTML 413. `tests/test_frontend_nginx.py` pins it.
- **Deviation from the plan, named.** D7 as written also resolves (a) an id-less board with a
  known fingerprint and (b) an unrecognised non-`unknown` id with a known fingerprint. Both
  were previously "not layout-checked" / "refused as unsupported". Two R3-fw-5/R2b-be-7 unit
  tests encoded the old outcome; they now use an unknown table for their original intent,
  and new tests pin the resolution. Real firmware announces the id it detected by
  fingerprint, so neither case arises from a current build.
- **Release note.** Migration `0007`. The ingestor never fails a message on a `0006` schema
  (savepoint), but detects nothing until the api has migrated: restart
  `fleetforge-ingestor` after the api rollout, as for be-5/be-6. Its retained replay then
  records every board already on an unknown map (seen on dev: three old boards on the
  R3-fw-5 wrong-layout fixture). **The downgrade drops operator profiles and adoptions**,
  unlike 0005/0006.
- **Rejected.** Per-group profiles (D1). Deriving "detected" at read time from `devices` with
  no row (the acceptance wants a pending row, and the row keeps the first device, slot and
  flash size). Inserting from `store.py`. A DB trigger for builtin immutability (the API is
  the only writer besides migrations and the ingestor's `DO NOTHING`). Syncing builtins
  from code at startup (migrations own data). Server-side fingerprinting of an uploaded
  `partitions.csv` (useful follow-up, out of scope). A new SSE event type (`device.announce`
  already fires in the detecting transaction).
- **Proposed for the owner (`spec/` untouched).** `spec/device-protocol.md` → *Partition
  layouts*: a paragraph saying the table is the builtin set and a server may hold
  operator-defined profiles keyed by fingerprint, resolved for boards announcing `unknown`.
  Text in `docs/features/board-profiles.md` → *R3-be-2*.

---

## 2026-10-09 — The Arduino IDE package is generated: flattened src/, library.properties from library.json, LOG_LOCAL_LEVEL as a source prelude, no dot_a_linkage; proved by `just lib-arduino-check` on core 3.3.12 (R3-fw-8)

**Decided.** `scripts/arduino_package.py` (`just lib-arduino-package`) writes
`dist/arduino/Fleetforge/` and `dist/arduino/Fleetforge-<version>.zip` from the component
directory, which stays the one source. The output is a build artifact, gitignored like
`.pio/`, at the repo root (never under `agent/`: `check_bundles_fresh.py` reads git status
there, and `agent/dist/` holds agent bundles only). The R3-fw-3 "rejected: a generator" was
about a second checked-in source tree; this is not one. Details and the T2 table:
`docs/features/ota-library.md` → *R3-fw-8*.

- **Flat `src/`** = every file of the component's `include/` + `src/`, same basenames. A
  basename collision or a subdirectory is an error (none today).
- **`library.properties` is generated from library.json** (name, version, description,
  `headers` → `includes=Fleetforge.h`), `architectures=esp32`. No fourth version copy, and
  still none at the component root (the IDE would then accept the unflattened directory and
  fail on `include/`). **Never `dot_a_linkage` or `precompiled`:** the strong
  `verifyRollbackLater()` must be linked as an object, not pulled from an archive by
  resolution order. Test-pinned; T2 reads `T verifyRollbackLater` in both ELFs.
- **No per-library flags in the IDE.** library.json's `-DNAME=VALUE` flags become a prelude
  prepended to every packaged `.c`/`.cpp` (`#ifndef/#define/#endif`, value parsed from
  library.json), ending in a bare `#line 1`, so diagnostics keep the source's line numbers
  AND its installed path. `-Wall/-Wextra/-Werror` have no equivalent: the proof compiles
  with `--warnings all` (core 3.3.12: `-Wall -Wextra`) and fails on any warning from the
  library or the sketch. Any other flag stops the packager. The `#ifndef` form was enough:
  the core's `esp32-hal-log.h` defines `LOG_LOCAL_LEVEL` only `#ifndef`, and the prelude
  comes first; the `#undef` fallback was not needed. Every other byte is the source's.
- **Example:** `examples/Basic/{Basic.ino,partitions.csv}`, byte-identical. Not
  `platformio.ini` (its `symlink://../..` is wrong in a package), not the PlatformIO README,
  not `basic_idf/`. The sketch-local table is how the IDE gets `ab-4m-arduino-v1` (R3-fw-1).
- **Zip:** single root `Fleetforge/`, sorted entries, fixed 1980-01-01 timestamps and
  modes: same inputs, same bytes (test-pinned).
- **The proof is a harness**, `scripts/arduino_ide_check.py` (`just lib-arduino-check`):
  arduino-cli 1.5.1 pinned by URL + sha256 (checked before extracting), `esp32:esp32@3.3.12`,
  all in `/tmp/ff-arduino-ide` (build cache included), deleted at the end unless
  `--keep-toolchain`; refuses to install with < 10 GB free. It packages into a temp dir,
  `lib install --zip-path`, copies the INSTALLED example (*Save As*) and compiles it with the
  default menu for `esp32:esp32:esp32` and `esp32:esp32:esp32s3`. `compile --format json`,
  because arduino-cli 1.5.1 prints the *Used library* table only with `-v`.
  `--negative-control` strips the prelude and must fail check (h).
- **Measured.** Core install 148 s, 16 → 14 GB free during, back to 16 GB after teardown.
  Apps: esp32 1165520 B, esp32s3 1156304 B (slot 1966080 B); `Maximum is 1310720 bytes` on
  both. Negative control: esp32 app 1152576 B (12944 B of INFO strings gone), check (h)
  FAILS, everything else passes. Injected unused variable in the sketch → reported in
  `compiler_err` with the sketch path (the warning gate sees ours).
- **Rejected.** A checked-in `library.properties` at the component root; restructuring the
  component to `src/` only (would erase the IDF component's public/private split); shipping
  `platformio.ini` in the package; `dot_a_linkage`; a `#line 1 "<basename>"` prelude (it
  would hide the installed path the warning gate keys on).
- **Named gaps.** (1) Arduino 1.5 format puts all of `src/` on the include path, so the
  private headers (`ff_mqtt_internal.h`, ...) are includable by a sketch, as with PlatformIO;
  the CMake `PRIV_INCLUDE_DIRS` protection is IDF-only. (2) The IDE's size gate is the board
  menu's `upload.maximum_size` (1310720 on the default scheme), not the 1966080 slot: the
  ~1.17 MB app passes today; past 1310720 the IDE refuses a build that fits the slot
  (workaround: PartitionScheme "Minimal SPIFFS"; the sketch-local table still wins).
  (3) `library.properties` cannot pin a core; 3.3.12 is stated in `paragraph` and the
  README only, and a core bump re-opens the R3-fw-1 measurement. (4) Not on the Arduino
  Library Manager (out of scope); release-zip publication belongs to R3-rel-1.

---

## 2026-10-09 — CUJ-1's Driver names the library harnesses: `just lib-quickstart` for steps 1-2, 3, 5, 6 on the device, `just update-e2e` for the dashboard half and the wrong-layout row (R3-spec-2, proposed)

**Decided: this entry covers the PROPOSAL only. Nothing is built: no code, test, recipe or
script change, and `spec/` is untouched; the owner applies it in a separate `spec:`
commit.** The reasoning, the sources and the paste-ready Patch A ((a)-(d), all
`spec/cujs.md`) are in `docs/features/ota-library.md` → *CUJ-1 Driver proposal*.

- **The rows.** 1–2: `just lib-quickstart --build-only` (the README blocks verbatim in a
  clean temp tree: Arduino esp32/esp32s3 + the edited build, ESP-IDF esp32/esp32s3).
  3: `just lib-quickstart` phase 2a (enroll), plus `pytest tests/test_enroll.py` for
  `POST /v1/enroll`. 5: on the device phase 2b (OTA, `fw_version` read back from the
  board); server and dashboard `just update-e2e` `upload-good` + `deploy-good`. 6: on the
  device phase 2c (`phase_rollback`: requires `rolled_back` and the previous `fw_version`,
  fails on any `confirmed`); server and dashboard `just update-e2e` `deploy-broken`.
  Wrong layout: `just update-e2e` `precheck-wrong-layout`; its on-device half is the
  `agent-qemu.md` procedure. One full `lib-quickstart` run plays 1–2, 3, 5, 6 on one
  emulated board (needs the api on `10.0.2.2` origins).
- **A `Graded` column.** The spec says a segmented CUJ "declares which segments are
  gradeable"; the table did not. `yes` everywhere except the wrong-layout row: `server and
  dashboard half`. Phases are named by the script's own headers (`phase 2a/2b/2c`) so
  `/verify` can attribute one run's output to rows 3, 5 and 6.
- **Dropped:** `just agent-qemu esp32` (interactive: boots and waits) and `just
  agent-qemu-smoke esp32` (boot + config read, not enroll; the stock agent, not the sketch)
  from row 3; `just sim-fleet 1 --capabilities ota` + `POST …/deploy` from row 5
  (`update-e2e` does that through the real dashboard).
- **Two Judge fixes, separable.** (b) `rolled-back` → `rolled_back`: the wire/API deploy
  state (`device-protocol.md` `rolling_back → rolled_back`) and what `phase_rollback`
  requires; a judge grepping the hyphen never matches. (c) Step 5's "read from the running
  image's own descriptor" is false on the persona's path: in an Arduino library build
  `esp_app_desc_t.version` is the core's IDF string and `fw_version` is the
  `Fleetforge.begin(FW_VERSION)` literal compiled into the image
  (`ff_identity_fw_version`, `#if defined(ARDUINO)`). Reworded to "the running image itself
  (its app descriptor, or in a library build the version compiled into it and handed to the
  library at start)". `R3-test-2` needs (a), and (b)+(c) for steps 5-6 to be judgeable.
- **The bench sentence** ("proved at the bench") is stale since the 2026-10-08 no-bench
  decision; replaced by "done by a person; no harness plays them and T3 does not grade them".
  (d), optional: Supported By drops the deleted `docs/runbooks/upload-artifact.sh` for the
  dashboard upload form.
- **Named gaps.** "A duplicated deploy command produces one download, not two" has no
  scripted harness (shown once by hand in R1-fw-1); T3 lists it, does not score it.
  Suggested follow-up, **not filed**: re-publish the same `dn/cmd` in `phase_ota` and
  require one `downloading` step and the `duplicate command` console line. The wrong-layout
  row's on-device half is a procedure. The IDF flavour is compile-only; QEMU boots the
  hybrid build. Side finding, not patched: `spec/standards.md:148` also says `rolled-back`.
- **Rejected.** Extra rows per half (splits one journey step in two). Keeping `agent-qemu`
  as a driver (interactive). `sim-fleet` + curl for row 5 (`update-e2e` covers it through
  the dashboard). Writing the duplicate-command gap into the spec (the spec is status-free).
- **Supersedes** the Driver-table parts of the R3-fw-4 proposal (b) and of the R2b-test-3
  spec proposal; both are folded in here.
- **Dependents blocked on the owner:** `R3-test-2`.

---

## 2026-10-09 — The library's rollback proof is an FF_ROLLBACK_TEST build of the QEMU sketch, set with PLATFORMIO_BUILD_FLAGS; the run fails on any `confirmed` (R3-test-1)

**Decided.** `just lib-quickstart` gains a third run, `phase_rollback`, after the OTA and on
the same board: bundle R (`"BAD"`, `1.2.0-qs<epoch>-rbtest`) built with
`PLATFORMIO_BUILD_FLAGS=-DFF_ROLLBACK_TEST=1`, deployed `on_command`, booted by a power
cycle, and required to roll back to B and report `rolled_back`. Details and the T2 table:
`docs/features/ota-library.md` → *R3-test-1*; procedure: `docs/runbooks/agent-qemu.md` →
*The worked example, end to end*, step 5.

- **The hook, not a sketch-level crash.** `ff_mqtt.c`'s existing `FF_ROLLBACK_TEST` image
  runs our code, joins the fleet, reports `confirming`, and is rolled back by the
  **library's confirm timer**, the CRITICAL path a maker depends on (and it proves
  `Fleetforge.cpp`'s `verifyRollbackLater()` override: without it, `initArduino()` would
  confirm R before `setup()`). A crash before the session would exercise only the
  bootloader's ABORTED path; that is a possible later addition, not this proof.
- **The env var is fine here; it was rejected for build B.** DECISIONS R3-fw-4 rejected
  `PLATFORMIO_BUILD_FLAGS` for B because B is a README step and the script must do what the
  reader does. No reader ever builds a rollback-test image: R is fault injection. A and B
  are built with the variable removed from the env. The flag is not trusted: R's `app.bin`
  must contain the hook's log string before upload.
- **`rolling_back` is reported, not required.** `ff_mqtt.c` sends it best effort within a
  2 s grace and never waits; requiring it would add flake unrelated to correctness. Required:
  `confirming` and `rolled_back` in the steps, terminal `rolled_back`, `fw_version` == B,
  the slot-naming detail, and no `confirmed` anywhere (API state/steps on every poll, R's
  console, the final row's version).
- **Slots are read, never hardcoded.** B's and R's slots come from their `is staged and
  bootable` lines (cmd_id in the regex); they must differ, and the expected detail is
  `returned to <B slot>; <R slot> did not confirm`. Observed: B `ota_1`, R `ota_0`.
- **Measured.** The R bundle took 32 s: the changed flags rebuild the project and the
  library, not the ~11 min framework step (pioarduino keys that on `sdkconfig.defaults`).
  Whole run 1512 s.
- **Rejected.** Editing `lib-qemu/platformio.ini` (pinned to the example's `env:esp32`), a
  hook in `Basic.ino` (makers copy it), a `--skip-rollback` flag (the acceptance is all
  three runs in one invocation).

---

## 2026-10-09 — The example's platformio.ini is the PlatformIO recipe; bare `pio run`; own project = copy three files + one `lib_deps` line; fresh core proved; git-URL form deferred to R3-rel-1 (R3-fw-7)

**Decided.** No new recipe file: `examples/Basic/platformio.ini` (keys unchanged, pinned by
`tests/test_arduino_library.py`) is the recipe, and `pio run` builds both targets via
`default_envs`. A maker's own project copies `Basic.ino`, `partitions.csv` (unchanged, never
edited) and `platformio.ini`, and changes one line, `lib_deps`, to a `symlink://` to the
clone's `agent/components/fleetforge`. `scripts/lib_quickstart.py --fresh-pio-core`
(`--build-only` only) runs the README blocks on an empty `PLATFORMIO_CORE_DIR` and proves the
pinned pioarduino 55.03.312 installs from nothing. Phase 0 stays the `git ls-files` copy (a
HEAD clone would test the old README). Details and the T2 table: `docs/features/ota-library.md`
-> *R3-fw-7*.

- **Deferred.** The GitHub git-URL `lib_deps` form: `git+file://` installed
  `Fleetforge@0.4.7+sha.3affea2` (PlatformIO finds the nested `library.json`), but
  `origin/main` is behind and no tag carries the library. Noted under R3-rel-1.
- **Rejected.** A second script for fresh-core mode; a registry publication (out of scope).

---

## 2026-10-09 — Layout detected by fingerprint; no match announces `unknown`; `unsupported_layout` refusal names the expected layout, slot size and the fix; rollback guard moved into the component's OTA path (R3-fw-5)

**Decided.** A board says which map it carries, and a board on a map nobody supports is
refused at deploy time with a sentence that tells a library user what to do. Details and
the T2 table: `docs/features/ota-library.md` → *R3-fw-5*; procedure:
`docs/runbooks/agent-qemu.md` → *A wrong flash layout is refused*. Supersedes the R3-fw-3
named gap "the layout id is chosen by `ARDUINO` at compile time".

- **D1. The firmware detects the layout by fingerprint.** `ff_identity.c` holds every known
  id with its fingerprint (`FF_KNOWN_LAYOUTS`, both rows, not just the build's own), and the
  announced `partition_layout` is the id whose fingerprint equals the measured
  `partition_table_sha256`. `tests/test_layout_detection.py` keeps the C table equal to
  `SUPPORTED_LAYOUTS`. `FF_PARTITION_LAYOUT` stays byte-for-byte (pinned by
  `tests/test_arduino_library.py`) and now means "the layout this build ships": it is the
  expected layout in the log line and the fail-open fallback.
- **D2. No match announces the reserved id `"unknown"`.** `FF_PARTITION_LAYOUT_UNKNOWN` in C,
  `manifest.UNKNOWN_PARTITION_LAYOUT` in Python, test-pinned equal and never a
  `SUPPORTED_LAYOUTS` key.
- **D3. A table that cannot be measured keeps the build's id**, with the fingerprint null, so
  the server's fail-open (R2b-be-6/7) holds. Exception: more than 32 entries
  (`FF_PT_MAX_ENTRIES`) is proof the table is not a supported one (they have six rows), so
  `s_pt_too_many` announces `unknown`.
- **D4. One loud `ESP_LOGE` (tag `ff-id`) at boot when the board announces `unknown`.** It
  names the running OTA slot size, the build's expected layout and its slot size, that the
  server refuses every update, and the fix (the example's `partitions.csv`, flashed once
  over USB; a table never changes over the air). No 12-character hex run in it, because
  `frontend/src/diagnostics.ts` reads the device id from the last `ff-id` line that way.
  The `board:` INFO line gains `layout %s`.
- **D5. No device-side stage refusal based on the layout.** A server refusal can be fixed by
  shipping server code; a refusal baked into firmware cannot be fixed on that board at all,
  because OTA is what it refuses. `choose_target_slot` already refuses a missing spare slot
  and esp_https_ota refuses an image larger than the slot.
- **D6. Server: a new stable refusal code `unsupported_layout`.** Emitted when
  `device.partition_layout` is set and not in `SUPPORTED_LAYOUTS`, with or without an
  artifact, and also when the artifact's layout is null (before, an unknown-layout board
  plus a layout-less artifact passed every check). It replaces `layout_mismatch` for that
  board, the fingerprint check is skipped (no profile), it is never overridable, and
  `/deploy` answers 409 through the existing generic mapping. Order: `no_artifact_for_target`
  → `unsupported_layout` | `layout_mismatch` → `slot_too_small` → `partition_table_mismatch`
  → `no_ota_capability`. `layout_mismatch` between two supported layouts is unchanged.
- **D7. The sentence.** Plain text, no backticks. It names what the board announced (layout,
  slot, fingerprint when present), the expected layout and its slot size (the artifact's
  when it is a supported id, otherwise every supported id), and the fix with a source hint
  from `deploy_precheck.LAYOUT_SOURCES` (keys pinned equal to `SUPPORTED_LAYOUTS`'). The
  `partition_table_mismatch` sentence keeps its text as a prefix and gains the same fix
  (the library 0.4.7 case: compiled id, wrong table).
- **D8. "Does not start its OTA path on a bootloader without rollback" is a component build
  guard.** `ff_ota.c` has `#if !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE #error` next to the
  flash-encryption guard. Every consumer's bootloader comes from the same sdkconfig as its
  app, and `ff_ota.c` is always compiled, so this covers an IDF `main` that skips
  `fleetforge_start.c` and the agent itself (`agent_main.c` had no guard). The two consumer
  guards stay. The runtime posture is unchanged (`TXN_BOOTED_NEW` → `confirmed` with a
  detail, R2b-fw-2), and `rollback_capable` is still `true` or null (A3).
- **Rejected.** `null` for no match (the server reads null as "not reported" and fails
  open: the silent path this task closes). A fingerprint-derived id like `custom-47db…`
  (the fingerprint is already its own field). Keeping the compiled id (the old behaviour).
  A device-side stage refusal (D5). A persisted "bootloader cannot roll back" NVS flag that
  refuses later stages (irreversible, on an observation no bench can make: QEMU always arms
  rollback; it contradicts A3 and would defeat the server's per-code `rollback_incapable`
  override). The IDE's `Maximum is N bytes` line as the check (R3-fw-1 measured it wrong).
- **Named gaps.** A board whose bootloader came from an earlier, different flash
  (`idf.py app-flash`) is only discovered after its first OTA and is not reported as
  `false` (A3). Every board on an unsupported map announces the same `unknown`, so R3-be-2's
  "detected profile" must key on the fingerprint, not the id.
- **Side findings, not fixed.** `diagnostics.ts`'s "reported id" regex already matches the
  `board:` line's fingerprint (pre-existing since R2b-fw-2). `frontend/src/UploadBuild.tsx`
  offers every fleet-reported layout, so a fleet of only `unknown` boards pre-selects
  `unknown` and the upload answers 400 "unknown partition_layout; this server understands: …".
- **Proposed for the owner (`spec/` untouched).** `spec/device-protocol.md` → *Partition
  layouts*: `unknown` is reserved (text in `docs/features/ota-library.md` → *R3-fw-5*,
  Patch A).

---

## 2026-10-09 — The worked example is the Basic sketch as a Morse blinker plus an ESP-IDF twin under examples/basic_idf; the README's named blocks are what `just lib-quickstart` runs (R3-fw-4)

**Decided.** One worked example, two flavours, and a script that plays their READMEs.
Details and the T2 table: `docs/features/ota-library.md` → *R3-fw-4*; procedure:
`docs/runbooks/agent-qemu.md` → *The worked example, end to end*.

- **Arduino (the persona): `examples/Basic/Basic.ino` became the Morse blinker in place.**
  The folder keeps its name. `tests/agent_src.py`, `lib-qemu/platformio.ini`, `just
  lib-build`, the CRITICAL.md row and the docs all pin that path, so renaming buys nothing.
  `Basic/partitions.csv` and `Basic/platformio.ini` are not edited. Two `#ifndef` lines say
  what it blinks (`MORSE_MESSAGE "SOS"`) and its version (`FW_VERSION "1.0.0"`). Each
  literal occurs once, so the README's `sed` hits exactly those two lines. The LED is
  `LED_BUILTIN` when the variant has one (the S3 DevKitC-1's RGB LED), else GPIO 2. It is a
  `static const`, not two `#define`s: PlatformIO's `.ino` converter preprocesses with
  `-fpreprocessed -dD`, sees both branches, and warns `"LED_PIN" redefined`.
- **ESP-IDF: a new project `examples/basic_idf/`** (snake_case; `library.json`'s
  `examples/*/*.ino` glob never sees it). `main/main.c` is the reader's part (one screen,
  `fleetforge_start()` first). `main/fleetforge_start.c`/`.h` is the boot ladder, copied
  unchanged: the C twin of `Fleetforge.cpp`'s `begin()` + task. The confirm timer comes
  first, the `#error` fires without rollback, and it reaches the public `include/` only.
  It uses `agent_main.c`'s order, minus `ESP_ERROR_CHECK`. `EXTRA_COMPONENT_DIRS` = `../..`
  registers the component as one component (the build log's `Component paths` lists
  `agent/components/fleetforge` and `basic_idf/main`, nothing under `examples/`). No
  `idf_component.yml` override was needed. `fw_version` = `PROJECT_VER` in the top
  `CMakeLists.txt`.
- **The IDF example ships `ab-4m-v1`**, row for row `agent/partitions.csv` (ff_cfg at
  0x12000). An IDF build of the component announces that id. It is a new flash-time
  immutable, already covered by the `examples/*/partitions.csv` glob and now named in
  CRITICAL.md, with its `sdkconfig.defaults` and `fleetforge_start.c`.
- **No public-surface change.** `include/` stays exactly what `agent_main.c` calls.
- **READMEs: one per flavour.** Every command a reader types for a build is in a fenced
  block whose first line is `# quickstart: <name>`. `scripts/lib_quickstart.py` (stdlib
  only) extracts the blocks by name and runs them verbatim in a clean temp copy of the tree
  (`git ls-files --cached --others --exclude-standard`). A missing block is a hard failure.
  `--build-only` runs the Arduino builds (A, the README edit, B) and the IDF builds (in the
  pinned image, nothing written to the host). It checks: zero warnings from the
  library/example, app < 1966080, decoded table == ab-4m-v1, resolved rollback on, no eFuse
  burn (exact names, `verify_bundle.py`'s rule: `CONFIG_SECURE_BOOT_V1_SUPPORTED=y` is a
  SoC capability), app version 1.0.0. The full run adds the board steps in QEMU.
- **The full run is the first library OTA.** The two board-only steps are substituted.
  USB flash → `just lib-qemu --fresh`. The self-reboot → `apply: "on_command"` plus a power
  cycle, because QEMU panics on `esp_restart()`. The OTA artifact is the QEMU (hybrid)
  build of the edited sketch, not the persona binary, which has no driver for QEMU's NIC
  and would correctly roll back. Build B's version is `1.1.0-qs<epoch>`, because a label is
  a promise about bytes and a rerun would get 409.
- **Plan correction: `just lib-bundle` runs `pio run` twice.** In a fresh `lib-qemu/` the
  first run reinstalls the framework and does the hybrid compile (pioarduino keys that on
  a hash in the project's `sdkconfig.defaults`, which a clean tree does not have). That
  changes the build checksum, so the next pio invocation wiped `.pio/build/esp32-qemu`.
  When that was `-t idedata`, `lib_bundle.py` found no `bootloader.bin`. The second run is
  the rebuild (~30 s), and a no-op afterwards. So every quickstart run pays the ~11 min
  hybrid compile once. The repo's own `lib-qemu/` keeps its matching hash and is not
  disturbed.
- **Dev-box note:** `docker compose … up -d api` also tried to pull `minio/minio`, because
  this box's MinIO runs from a local override. `--no-deps` recreates the api alone.
- **Rejected.** Renaming `Basic`. A public `ff_start()` now (additive later, see below).
  The Arduino core as an IDF component as "the ESP-IDF build". `PLATFORMIO_BUILD_FLAGS`
  for build B instead of the README's edit (the script must run what the reader does). A
  library-level README (R3-rel-1). Copying ignored build state into the clean tree to save
  the hybrid compile.
- **Named gaps.** The IDF flavour is compile-only; QEMU runs the Arduino one. In an IDF
  consumer the announce's `agent_version` is `PROJECT_VER` (`ff_identity.c`'s non-ARDUINO
  branch), and R3-fw-6's `lib_version` is the planned answer. The QEMU run uses the hybrid
  build. The IDF example cannot light the S3 DevKitC-1's RGB LED.
- **Side finding, not fixed (CRITICAL file):** `Fleetforge.cpp`'s header point 2 still says
  PlatformIO links the library as a static archive. The R3-fw-3 entry above says it does
  not.
- **Proposed for the owner (`spec/`/`design/` untouched).** (a) An additive public
  `ff_start()`-style entry in the component, so IDF users stop copying
  `fleetforge_start.c`. (b) `spec/cujs.md` CUJ-1 Driver row 1–2 harness → `just
  lib-quickstart --build-only` (R3-spec-2 files it). (c) The IDF `agent_version` gap →
  R3-fw-6.

---

## 2026-10-08 — The Arduino library is the component plus library.json and a Fleetforge.cpp consumer; Arduino's verifyRollbackLater is overridden; pioarduino 3.3.12 / IDF 5.5.5 (R3-fw-3)

**Decided.** `agent/components/fleetforge/` is also the Arduino library: `library.json`
(PlatformIO defaults `src`/`include`, so every `src/*.c` compiles) plus the wrapper
`src/Fleetforge.h` / `src/Fleetforge.cpp`, the component's second consumer. The example
`examples/Basic/` ships `partitions.csv` = `ab-4m-arduino-v1`. Details and the T2 table:
`docs/features/ota-library.md` → *R3-fw-3*.

- **Toolchain:** pioarduino `55.03.312-1` (Arduino-ESP32 3.3.12 on ESP-IDF v5.5.5), pinned
  by exact URL in both `platformio.ini` files. It needs **PlatformIO Core ≥ 6.2.0**; this box
  was upgraded from 6.1.19 (`uv tool upgrade platformio`).
- **Rollback posture, all three in `Fleetforge.cpp`:**
  1. a strong `extern "C" bool verifyRollbackLater(void) { return true; }`. Core 3.3.12's
     `initArduino()` otherwise confirms every PENDING_VERIFY image before `setup()`.
  2. `ff_mqtt_arm_confirm_timer()` before anything in `begin()` that can fail or return.
  3. `#if !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` → `#error`. This is the "fails at build"
     clause. `rollback_capable` stays measured-only (2026-10-05), and the runtime
     "don't start OTA on a rollback-less bootloader" is R3-fw-5.
- **Plan correction: no archive linking.** pioarduino forces `lib_archive = False`, and the
  Arduino IDE links library objects directly. So the override is in every build that
  includes the library, not only builds that call `begin()`. Without `begin()` an OTA'd
  image is never confirmed and rolls back at its next reset. That is the conservative
  direction, and it is accepted. R3-fw-6 should read "a sketch that merely has the library
  installed" as "a sketch whose build includes it". `--gc-sections` still drops what nothing
  references.
- **Arduino differences only under `#if defined(ARDUINO)` in `ff_identity`.**
  - `FF_PARTITION_LAYOUT` is `ab-4m-arduino-v1`.
  - A private `ff_identity_set_app_versions()`, called only by `begin()`, supplies
    `fw_version` (the maker's literal) and `agent_version` (`FF_LIB_VERSION`).
  - The IDF agent's app is byte-for-byte the same size (1029504 B).
  - The R3-spec-3 side finding, re-measured on 3.3.12: `esp_app_desc_t.version` is
    `6671d0b` (the lib-builder's hash).
- **Version:** `src/ff_lib_version.h` is a test-pinned copy of `agent/version.txt`, and so is
  `library.json`'s `version`. Bump all three together (R3-rel-1's bump trips the test by
  design).
- **Logging:** the stock core compiles `ESP_LOGI` out (`CONFIG_LOG_MAXIMUM_LEVEL=1`) and sets
  `"*"` to ERROR before `setup()`. So `library.json` adds `-DLOG_LOCAL_LEVEL=ESP_LOG_INFO`
  (library sources only), and `begin()` raises exactly the 13 `ff-*` tags to INFO, pinned to
  the `TAG`s in `src/`. `CORE_DEBUG_LEVEL` is not used.
- **`-Werror`:** the core's global `-Wno-error=unused-*` / `-Wno-unused-parameter` /
  `-Wno-sign-compare` survive a library `-Werror`. IDF's defaults carry the same set, so
  this is parity with the CMake component, not a regression. The T1 gate rejects any
  `warning:` from the component.
- **QEMU is a separate project, `lib-qemu/platformio.ini` at the repo root** (example
  `env:esp32` + `custom_sdkconfig = CONFIG_ETH_USE_OPENETH=y`, pinned). It is run with its
  own `PLATFORMIO_CORE_DIR`, because the hybrid compile rewrites the shared Arduino libs in
  place: the next persona build printed `Reinstall Arduino framework` and re-downloaded it.
  The project dir is not inside the example, because the hybrid compile writes
  `.dummy/`, `managed_components/` and `sdkconfig.*` beside its `platformio.ini`. It is not
  inside `agent/` either, because `test_agent_holds_no_credential` scans `agent/` and found
  a `WIFI_SSID=` in a managed component. The hybrid config also differs from stock beyond
  the NIC (no SPIRAM/Matter/camera, 240 MHz, DIO). That is a named gap: QEMU proves the
  library's C on the core's IDF, while `just lib-build` proves the persona binary compiles.
- **`agent/tools/lib_bundle.py`:** offsets come from `idedata.json` plus the app's position
  in `firmware.factory.bin`. The table is decoded by `make_manifest.py`'s code. It refuses on
  otadata/ota_0/ff_cfg/slots/fingerprint. The id is written only when the geometry is
  `ab-4m-arduino-v1`. Output never goes to `agent/dist/`.
- **Rejected.**
  - The official `espressif32` 7.0.1 platform: IDF 4.4, so the component does not compile.
  - A top-level `arduino/` copy or a generator: a second source.
  - `library.properties` now: the IDE cannot see `include/`. Follow-up `R3-fw-8`.
  - Global constructors or `used`/`retain`: linking must do no work.
  - A runtime layout probe: that is R3-fw-5.
  - Vendoring IDF's openeth driver into the library.
  - An unguarded setter: it would move the agent's size.
  - A third env inside the example's `platformio.ini`: it pollutes the folder a maker copies,
    and the shared libs ping-pong.
- **Proposed for the owner (`spec/`/`design/` untouched).** Optional, in `design/architecture.md`:
  "The Arduino library is the same `fleetforge` component, with `library.json` and
  `src/Fleetforge.cpp`; its example carries `ab-4m-arduino-v1`."
- **CRITICAL.md:** two rows added. One covers `agent/components/fleetforge/examples/*/partitions.csv`
  as a flash-time immutable. The other covers `Fleetforge.cpp`'s rollback posture.

---

## 2026-10-08 — The protocol is the `fleetforge` ESP-IDF component under agent/components/; its public surface is exactly what agent_main.c calls (R3-fw-2)

**Decided.** The 12 `ff_*` modules moved out of `agent/main/` into
`agent/components/fleetforge/` (`git mv`, history follows). `agent/main/` now holds only
`agent_main.c`, unchanged byte for byte, and it is the component's first consumer
(`REQUIRES fleetforge`). Details and the T2 table: `docs/features/ota-library.md` →
*R3-fw-2*.

- **Layout.** `include/` is public: the 8 headers `agent_main.c` includes. `src/`
  is private (`PRIV_INCLUDE_DIRS`): every `.c`, `ff_ota.h`, `ff_txn.h`, `ff_net_adapter.h`,
  and five `*_internal.h` halves. The directory name is the component name, so it is never
  renamed. It sits **inside `agent/`** because `agent/` is the Docker build context, and
  because `check_bundles_fresh.py`'s pathspec `agent` (minus `agent/dist`) then covers it
  with no edit (a test now proves it). A top-level `components/` would be outside the image,
  and a change to it would not make a bundle STALE.
- **The rule, and how "strict subset" is read.** Every function declared in a public header
  is one `agent_main.c` calls (16), and every `ff_*` call in `agent_main.c` is declared
  publicly. The old headers exposed ~40 functions to `agent_main.c`. The public set is now
  exactly the 16 it uses, so no public function goes unused by the first consumer.
  Macros and types are public only when a public declaration needs them (`ff_cfg_t`,
  `ff_cred_t` and their `*_MAX_*`) or when they are a public function's closed vocabulary
  (`FF_PROGRESS_*`, the `ff_cfg` binary-format constants the flasher pins).
  `tests/test_ota_component.py` pins all of it. The public surface is **additive-only**
  from this commit, like the wire protocol. TODO's "four verbs, enroll,
  announce/heartbeat, version accessor" maps onto it as follows. The verbs are
  `ff_mqtt_arm_confirm_timer` + `ff_mqtt_run`: stage, apply, confirm and rollback all happen
  inside the session. Enroll is `ff_enroll` plus the cfg/store/time/net/identity calls
  around it. Announce and heartbeat happen inside `ff_mqtt_run`. The version accessor is
  `ff_identity_fw_version`. `ff_progress_*` is an accepted extra, because the agent calls it.
- **Kept private on purpose** (none of these could be withdrawn once shipped):
  `ff_mqtt_publish_status` + `FF_STATUS_*` would let a firmware publish `confirmed` for an
  image that never confirmed. `ff_identity_note_rollback_capable` /
  `ff_store_save_rollback_capable` would let it *claim* `rollback_capable` (measured, never
  claimed: 2026-10-05 R2b-fw-2). `ff_identity_announce_json`, `FF_PARTITION_LAYOUT` and
  `FF_OTA_SLOT_SIZE` would turn the announce and the three-way layout contract into user
  API. The NVS namespace/keys, `ff_net_link_type/rssi/ssid` and `ff_time_is_sane/iso8601` are
  private too.
- **Version single source: `agent/version.txt`.** It is already the release-bumped number,
  and it feeds `PROJECT_VER`. In the agent build `agent_version`, `fw_version` and the future
  `lib_version` are that one number. `idf_component.yml` has **no `version:`** (optional for
  a local component, and leaving it out means no second copy to drift). Adding it is
  additive, when registry publication is in scope. How the number reaches `ff_marker.c` is
  `R3-fw-6`'s mechanism. Arduino (`R3-fw-3`) has no CMake, so it may need a generated or
  test-pinned header. `agent_version` in the announce stays `esp_app_get_description()->version`.
- **`idf: ">=5.5"`**: v5.5.5 is the only IDF ever built, and loosening it later is
  additive. The component manager runs and resolves only `idf`, with no registry access and
  no error. Its `dependencies.lock` is written inside the build container and never reaches
  the host.
- **IDF deps are `PRIV_REQUIRES`.** The public headers need only `esp_err.h` and
  `freertos/FreeRTOS.h`, so nothing leaks into a consumer's include path. `main` lists what
  `agent_main.c` includes directly.
- **FF_ROLLBACK_TEST moved with `ff_mqtt.c`** into the component CMakeLists. A define on
  main's target never reaches the component's objects, and an `FF_ROLLBACK_TEST=1` build
  would then silently confirm, so the rollback test would prove nothing. FF_FAULT_TEST stays
  in `main/CMakeLists.txt` with its hooks in `agent_main.c`.
- **CRITICAL files are include-only.** `ff_mqtt.c` (2 lines) and `ff_ota.c` (1 line)
  changed only `#include "ff_X.h"` → `"ff_X_internal.h"`. `ff_txn.c` is a 100% rename.
- **Size budgets** were raised to the exact measured byte: esp32 +64 B, esp32s3/c3/c6 +48 B.
  No code changed. The `__FILE__` strings that ESP_ERROR_CHECK/assert embed grow from
  `./main/ff_*.c` to `./components/fleetforge/src/ff_*.c` (+21 B each), rounded up by
  alignment.
- **T2, in short.** All four `just agent-build` runs ended `BUNDLE OK`, with zero warnings.
  `sdkconfig.resolved`, `config_sha256`, the partition-table and otadata sha256 are identical
  to the 0.4.7 baseline on all four targets. In the app `strings` diff, the only text that
  changed is those source paths and the build date/time. In QEMU, the component build's
  announce matches the baseline's key for key and in key order. OTA to B confirmed
  (`staged, confirming, confirmed`). A `FF_ROLLBACK_TEST=1` R rolled back by the confirm
  timer (`… confirming, rolling_back, rolled_back`, no `confirmed`), and the board was back on B.
- **Rejected.** Keeping the old headers whole (makes the hazards above public forever). A
  top-level component dir (outside the build context and the freshness pathspec). An
  umbrella `fleetforge.h` (`agent_main.c` would not include it, so it breaks the subset
  rule). `ff_lib_version()` now (unused by the agent, and a boot line would be a behaviour
  change). Editing `agent_main.c`.
- **Proposed, path-only, for the owner (`spec/`/`design/` untouched):**
  `spec/open-questions.md:50` and `design/partitions.md:122`: `agent/main/ff_cfg.c` →
  `agent/components/fleetforge/src/ff_cfg.c`.
  `design/decisions/enrollment-console-is-the-diagnostic-surface.md:43`: `agent/main/*.c` →
  `agent/main/*.c and agent/components/fleetforge/src/*.c`. Optional, `design/architecture.md`:
  one sentence saying the device code is the `fleetforge` component plus the agent app, and
  that the component's `include/` is additive-only like the wire protocol.

---

## 2026-10-08 — The library marker is a 64-byte constant found by scanning the image, not a field of esp_app_desc_t; boards announce it as lib_marker (R3-spec-3, proposed)

**Decided: this entry covers the PROPOSAL only. Nothing is built: no agent, server,
frontend, migration, simulator or test change, and `spec/` is untouched; the owner applies
it in a separate `spec:` commit.** The design, the spike and the paste-ready patches are in
`docs/features/ota-library.md` → *Library marker proposal*.

- **Layout.** One 64-byte, 4-byte-aligned `const` struct in the library's shared C core
  (planned `ff_marker.c`, in the `R3-fw-2` component, so the Arduino library carries it too):
  `magic` 16 B = `14a948d18f12cfdd46464f54414c4942` (8 random bytes, then ASCII `FFOTALIB`;
  frozen, never regenerated), `format` u8 = 1, 3 reserved, `lib_version` char[32]
  (printable ASCII, NUL-terminated, non-empty), 12 reserved. Bytes and chars only, so no
  byte order.
- **Reader rule (server).** Scan the whole image for the magic; accept the first occurrence
  with 64 bytes to the end of the image, `format >= 1` and a valid `lib_version`; read later
  fields only when `format` says they exist. None valid is "no marker"; malformed is "no
  marker", never an error. One `bytes.find` loop over at most 1966080 B per upload.
- **Writer rule (library).** Defined once; kept alive only by an `extern` read from library
  code (the announce builder and/or the confirm path), so `--gc-sections` keeps it exactly
  when that code is linked. **Never** `-u`, `KEEP()`, `used` or `retain`: a force-kept marker
  would mark a sketch that merely has the library installed. The read should let the address
  escape (pass `lib_version` as a string), since a lone scalar read can be folded under LTO
  (LTO unmeasured). Growth is additive: reserved bytes plus a higher `format`.
- **What it proves:** the library's code, including its confirm timer, is linked. **Not**
  that the firmware starts it, not that the layout is right (`partition_layout` and the
  fingerprint), not that its logic works (R5 self-test).
- **Why not `esp_app_desc_t`.** The maker owns `project_name` (their CMake project) and
  `version` (becomes `fw_version` and pre-fills the upload form, `frontend/src/appImage.ts`);
  and an Arduino library cannot write it: the spike's Arduino builds carry
  `version='esp-idf: v4.4.7 38eeba213a'`, `project_name='arduino-lib-builder'` from the
  core's precompiled libraries. No free field.
- **Why not `.rodata_custom_desc`, a section or an ELF note.** The custom descriptor is the
  maker's single slot, depends on every core's linker script placing it, and is kept with
  `-u custom_app_desc`, which an Arduino library cannot add. A dedicated output section
  needs a linker-script change. The server receives the `.bin`, which has no symbols. The
  scan needs no linker cooperation: an app `.bin` holds `.rodata` verbatim and a `const`
  object is never split across segments.
- **On the wire: `lib_marker`**, one additive flat `up/announce` integer, the `format` of
  the marker in the **running** image; absent on images without one, which is every agent up
  to 0.4.7. `proto` stays 1; +15 B on about 590 B. The server never refuses or warns on a
  device for it (`null`/absent never warns); malformed (not an int in 1..255) is stored as
  null. The pre-check gates on the marker of the **image being sent**, never on the board.
- **The stock agent carries it** from the first agent built on the component ("has the
  confirm path, has the marker"). `R3-fw-6`'s "the stock agent and every pre-R3 board
  announce none" now reads "agents ≤ 0.4.7 announce none" (clarified in `TODO.md`).
  `R3-be-1`'s "not for the stock agent's own bundles" stands: they go through
  `firmware/publish.py`, never `POST /v1/artifact`.
- **`R3-be-1` inherits:** code `no_library_marker`, a gating warning (`needs_override:
  true`), the second after `rollback_incapable`; `/deploy` answers 409 unless
  `override: ["no_library_marker"]`; the sentence lives in `deploy_precheck.py`. The verdict
  is computed at upload and stored, because a deploy never reads bytes. Storage is left to
  `R3-be-1`: `artifacts.provenance` is producer-copied identity ("copied, never
  recomputed"), a poor home; a new column is an alembic migration (CRITICAL). Artifacts
  uploaded before `R3-be-1` have no verdict and never warn (named gap).
- **Version note for `R3-fw-2`/`R3-fw-6`.** `lib_version` is the component's version;
  `R3-fw-2` picks its single source. In a library build `agent_version` is that same
  constant and `fw_version` stays `esp_app_desc_t.version` (`ff_identity.c`: "only
  `fw_version` moves").
- **Side finding, not fixed (`R3-fw-3`/`R3-fw-4`).** In an Arduino build (core 2.0.17)
  `esp_app_desc_t.version` is the core's IDF string, so `fw_version` and the upload form's
  version pre-fill are wrong for every Arduino build until the library supplies the maker's
  version another way. Noted on `R3-fw-3` in `TODO.md`. Core 3.x not measured.
- **Application order.** Patch A ((b) `## Library marker` section in `device-protocol.md`,
  (c) the `lib_marker` prose, (d) the `flows.md` line 144 deletion, (e) the
  `open-questions.md` candidate (1) rewrite) any time after the owner accepts. Patch B ((a),
  `"lib_marker": 1` as the announce example's last key) **only in `R3-fw-6`'s commit**:
  `TestAnnounceMatchesTheSpec` goes red otherwise. Dry-run on a repo copy: A keeps the
  spec-reading tests green; A + B fails exactly that one test.
- **Spike (PlatformIO 6.1.19, `espressif32` 7.0.1, `esp32dev`; a mini library whose
  `fflib_begin()` reads the marker by `extern`).** Arduino `call`: `marker format=1
  lib_version=0.0.0-spike @0x148`; Arduino `nocall`: `no marker`; ESP-IDF 6.0.1 `call`:
  `marker format=1 lib_version=0.0.0-spike @0x5a74`; ESP-IDF `nocall`: `no marker`. Zero
  false positives: the four 0.4.7 `agent/dist/*/app.bin` and the four
  `tests/fixtures/firmware/*.bin` all scan as `no marker`.
- **Dependents blocked on the owner:** `R3-fw-6`, `R3-be-1` (marked in `TODO.md`).

Supersedes nothing. Answers the open item in `spec/flows.md` (Flow 2 decisions) and
candidate (1) of *An OTA image that does not contain the agent* in
`spec/open-questions.md`, once Patch A is applied.

---

## 2026-10-08 — No hardware bench tests; R3 opens with QEMU/simulator-only acceptance

**Decided: fleetforge stops filing and keeping tasks whose acceptance needs a person at a board. Removed: `S0-test-1`, `S0-test-2`, `R2b-test-2`, `R2b-test-4`, `R2b-test-5` (TODO), `scripts/bench_judge.py`, `scripts/bench_net.py`, their tests, the `bench-judge` and `bench-net` recipes, and the `bench-replay`, `known-networks-bench` and `serial-console-bench` runbooks. R3 (thin OTA library) is open, task list in `TODO.md` → *R3*.**

- **Why.** The bench was one Windows + Chrome machine with one native-USB S3, which is offline (`S0-bug-1`) and cannot run the bridge-chip checks. The hardware-gated tasks sat open for weeks and blocked nothing they proved. QEMU and the simulator already carry every recovery path (R2-test-1/2, R2-fw-4/5).
- **Supersedes** the 2026-10-04/05 bench entries below (`bench-replay`, `bench-net`) and every "bench replay owed" line in `docs/features/` and `docs/runbooks/rollback-test.md`: retired, not pending.
- **Consequences, accepted.** `R2b-fw-2` keeps never emitting `rollback_capable: false` (`R2b-test-5` withdrawn); real-radio network selection and the native-USB re-acquire window stay unproven on metal. `frontend/src/fixtures/bench-*.ts` stay: they are recorded real logs used by jsdom tests, not bench tooling.
- **Kept:** `S0-bug-1` and `R2b-test-1` (a person at a board); not on R3's path. Withdraw if the new approach should cover them.

---

## 2026-10-05 — Moving a board between networks is graded from the device row by `just bench-net`; the bench session also checks stay-put, priority on boot and nothing-in-range; the run is held for the R2b release (R2b-test-4)

**Decided: `scripts/bench_net.py` (stdlib only, `just bench-net <device> <ssid> <known> [prod|dev] [timeout]`) polls one `devices` row and grades it; `docs/runbooks/known-networks-bench.md` is the bench session. The task stays `- [ ]`: the acceptance needs the Windows + Chrome bench with the ESP32-S3 and two real 2.4 GHz networks, and this pass has a rehearsal only (R2b-test-1 and R2b-test-2 precedent).**

- **Why the run is held.** (1) Prod is 0.4.2 / `9200e0f`: no migration 0005, so no `devices.ssid`, no fleet-row copy, no "Add another network" (`git merge-base --is-ancestor 1dfe042 <prod commit>` is the one check, since fe-13 descends from be-5 and fe-12). The release needs the owner. (2) The prod flasher serves esp32s3 0.3.2 (`S0-infra-10`), and a 0.3.x agent joins network 1 only; the esptool route (B) is the fallback. (3) `S0-bug-1`: its power-cycle diagnosis must come before a re-flash, which erases the evidence. (4) A second 2.4 GHz network at the bench.
- **Why the device row, not `deploy_events` or the API.** The server keeps no SSID history, so "the row change" has to be polled, and it is graded from the server's own record, not an operator's impression. psql over ssh as `bench-judge` does, so no admin credential is on the bench path. A judge over `GET /v1/devices` would need the admin password there.
- **Verdict rules.** Exit 0 PASS, 1 FAIL, 2 usage / refused device / no such device / fetch error, 3 WAIT (and `watch` timing out). FAIL is a wrong board or wrong flash, so waiting cannot fix it: link not wifi, agent below 0.4.6 (an unparseable version WARNs and skips), a reported `known_networks` that is not the expected count. WAIT is offline, or online on another SSID (including none). PASS is online + wifi + exact SSID + expected count; a NULL count with all else passing is FAIL. `online` is `presence_reported = true` (`presence.py::is_online` for `always_on`); a sleepy board WARNs. The SSID is compared in Python only, case-sensitive, and never enters SQL. The device id must `fullmatch` 12 hex (a `$` regex would let a trailing newline through) before any SQL or process exists. A missing `ssid` column prints "the R2b server (migration 0005) is not on it", so the checker doubles as the "R2b is on prod" precondition. `watch` prints the row only when it changes (the age is not part of the change) and `--timeout 0` reads once. The recipe passes `--ssid="$ssid"` so an SSID that starts with `-` survives argparse.
- **The session.** S1 only network 2 in range (the task's literal acceptance), S2 network 1 returns and the board stays (the spec's never-leave-a-working-association rule), S3 priority on a fresh boot, S4 the move, S5 nothing in range (no reboot, `NET WAIT`, back within about 35 s). **Still unproven after it:** the 20 s DHCP watchdog (needs an AP that associates but gives no lease), strongest-AP choice within one SSID (needs a mesh), direct tries of hidden SSIDs.
- **Route B** (esptool): mint a token on prod, build the blob with `agent/tools/ff_cfg.py --ssid/--psk/--net` (passphrases read with `read -rs`), flash at the manifest's offsets (`0x0` bootloader, `0x8000` table, `0xf000` otadata, `0x20000` app, `0x12000` ff_cfg), delete the blob. The esp32s3 0.4.7 bundle was rebuilt (`just agent-build esp32s3`, `BUNDLE OK`); `agent/dist/` is untracked, so the snapshot lives in the runbook (built from `8cf4475`, `app.bin` sha256 `675f5401…bd80a92`).
- **T2-R evidence (dev stack on 8088, simulator `--agent-version 0.4.7-sim --ssid … --known-networks 2`).** A PATH shim proved a refused device never invokes docker (and that the shim does catch a legal one).
  - `bench-net 0123456789ab net-a 2 dev 0` exit 2 `no live device 0123456789ab on this server`; `"x';--"` exit 2 `refused device`, docker never run; `where=elsewhere` exit 2 (argparse).
  - Sim on `net-a`: `... net-a 2 dev 60` -> `NET PASS on "net-a" (known network count 2)` exit 0; `... net-b 2 dev 5` -> `NET TIMEOUT after 5 s: the board is online on "net-a", expected "net-b"` exit 3; `... net-a 3 dev 0` -> `NET FAIL the board reports 2 known networks, expected 3: re-flash with 3` exit 1.
  - **The move with a half-open old session:** `kill -STOP` on the first sim's python, a second sim on `net-b` -> `... net-b 2 dev 60` `NET PASS`. After `kill -9` of the stopped process and 15 s, `... net-b 2 dev 0` was still `online, on: "net-b"`, `NET PASS` exit 0. The old session's last-will did not leave the row offline: no finding, no S0 task.
  - Stopped cleanly: `... net-b 2 dev 0` -> `offline, last on: "net-b"`, `NET WAIT the board is offline (last on "net-b")`, exit 3. A watch started before the sim came back printed the transitions `offline, last on: "net-b"` then `online, on: "net-a"`, then `NET PASS`.
  - Route B: `ff_cfg.py --link wifi --ssid … --psk … --net … …` wrote 4096 bytes; neither dummy passphrase appears in its output or in `--print`; the blob was deleted.
  - Not run: the optional read-only `just bench-net 94a990dd09a4 x 2 prod 0` (ssh to prod was refused by the permission layer in this pass, so the ssh path and the "no ssid column" message on real prod are proven only by the unit tests).
- **Rejected:** a judge over `GET /v1/devices` (needs the admin password on the bench path); storing an SSID history (a schema change for a test); QEMU (no radio); flipping the task on a rehearsal.

Supersedes nothing.

---

## 2026-10-05 — The deploy gate reads the board measurements: `partition_table_mismatch` is a device-vs-profile refusal, `rollback_incapable` is a gating warning cleared only by naming it in `override` (R2b-be-7)

**Decided: `deploy_precheck.py` gains the refusal `partition_table_mismatch` and the gating warning `rollback_incapable`; `DeployRequest.override: list[OverrideCode]`; `PrecheckFinding.needs_override`; `SUPPORTED_LAYOUTS` becomes `dict[str, LayoutProfile(ota_slot_size, partition_table_sha256)]`. Details and T2 evidence: `docs/features/ota-deploy.md` → *R2b-be-7*.**

- **The fingerprint check is device vs profile, independent of the artifact.** It compares the announced `partition_table_sha256` with the profile of the layout the device announces, so it runs even when the label names nothing for the chip, the same way the capability check does. Order: `no_artifact_for_target | layout_mismatch, slot_too_small`, `partition_table_mismatch`, `no_ota_capability`.
- **Known fingerprints only, NULL fail-open (R2b-be-6).** It refuses only when the device sent a fingerprint AND its layout has a known one AND they differ. An unknown layout, or a profile whose sha is `None`, is logged at INFO (`… not checked`) and passes. `rollback_incapable` fires on `rollback_capable is False` only; `None` is silent.
- **Gating is appended last, and `deployable` is unchanged** ("no refusals"); the card's `canSend()` already combines it with the ticks. The pre-check accepts `override` (same body) and ignores it, so it always reports the gating warning with `needs_override: true`.
- **The 409 sentence is the warning's own**, byte for byte, raised in `_accept` after the refusals (a refusal always wins; `override` never clears one) and before the reuse lookup, the mint and the INSERT: a gated deploy writes no row and publishes nothing.
- **`override: []` is legal** (the default; the card omits the key when empty). An unknown code is a 422 (pydantic `Literal`); a listed code that is not raised is a no-op. Max 8 entries.
- **`OverrideCode` is a literal in `api/schemas.py`**, so it cannot reference the constant; `tests/test_deploy_precheck.py::TestOverrideCodes` keeps it equal to `deploy_precheck.GATING_CODES`. The R3 library marker will be the second entry of both.
- **`LayoutProfile` is a frozen dataclass inside a plain mutable dict**, so tests can `monkeypatch.setitem` a row. `TestLayoutProfiles` keeps the dict equal to the spec's *Partition layouts* table; `tests/test_agent_partitions.py` pins the `ab-4m-v1` value against `agent/partitions.csv` by the spec's rule (retyped, imports nothing from `fleetforge`).
- **The Arduino sha comes from the ADR's table** (`design/decisions/arduino-gets-its-own-layout-id.md`): R3 has not checked in a CSV. No Arduino board announces a fingerprint today, so it refuses nobody; T2 row I is its positive control.
- **Observability.** The deploy log line carries `override=[…]`. The sentences are plain text (no backticks), shown verbatim in the banner and the card.
- **Patch B is still owed** as the owner's `spec:` commit (the `up/announce` example in `spec/device-protocol.md` lacks the three keys). The TODO line's "apply Patch B in this task's commit" is stale (DECISIONS 2026-10-05 R2b-fw-2); nothing here needs it.
- **Rejected:** recording `override` in `deploy_events.detail` (useful for an audit of who overrode what; a follow-up, out of scope here); a blanket `force`; making a gating warning set `deployable=false` (breaks the card); editing the stale frontend comments in `api.ts`, `deployPrecheck.ts` and `DeployCell.tsx` that say `needs_override` does not exist yet (be-6 precedent: no `frontend/` edits in a backend task; follow-up); an `ab-4m-v1` fingerprint check against the artifact (the artifact carries no table).

Supersedes, from 2026-10-04 (R2b-be-2): "warnings never block `/deploy`; no override field yet". Gating warnings now block unless overridden.

---

## 2026-10-05 — The agent announces the three measurements; `rollback_capable` is observed from any PENDING_VERIFY boot and stored in the credential namespace; a NEW-at-target boot ends `confirmed` with a detail; `false` waits for R2b-test-5 (R2b-fw-2)

**Decided: agent 0.4.7 emits `flash_chip_size`, `partition_table_sha256` and `rollback_capable` right after `ota_slot_size`, in spec order. Details and T2 evidence: `docs/features/board-profiles.md` → *R2b-fw-2*.**

- **Measured once, never claimed.** `ff_identity_init()` (after the MAC, never fatal) reads `esp_flash_get_physical_size(esp_flash_default_chip, …)` and fingerprints the table: `esp_partition_find(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY, NULL)`, entries on `esp_flash_default_chip` only, copied into a static 32-entry array, insertion-sorted by address, one decimal `type:subtype:offset:size\n` line each into mbedTLS SHA-256, hex by hand. No label, no flags. More than 32 entries or a hash error announces null. An unreadable chip size is **omitted** (the spec: "a device that cannot read it omits the field"); the sha and `rollback_capable` go out as explicit null, as `ssid` does. One boot line is the console proof: `board: flash chip N bytes (physical), partition table sha256 …, rollback_capable true|unknown`.
- **`rollback_capable` is observed at the top of `ff_mqtt.c::classify_txn()`, from `pending_verify()`, before the transaction record is read.** This is a deliberate superset of "from the `TXN_CONFIRMING` branch". Only the bootloader writes PENDING_VERIFY (NEW → PENDING_VERIFY), so booting in it is the measurement whether or not a record exists. The no-record case is prod's next deploy: the prod board runs 0.3.1, which writes no record (records arrived in 0.4.0), and restricted to `TXN_CONFIRMING` its first 0.4.7 boot would learn nothing. `TXN_CONFIRMING` implies PENDING_VERIFY, so the named branch is covered. Not observed in `confirm_timeout_cb`, `rollback_now_cb`, `rollback_report_task`, `ff_mqtt_arm_confirm_timer` or `confirm_this_image` (all unchanged and NVS-free). `TXN_ROLLED_BACK` and VALID are not evidence: `esp_ota_mark_app_invalid_rollback()` writes INVALID from any state, and VALID cannot be told apart from "confirmed, then reset before the PUBACK". `classify_txn` runs in the main task before `esp_mqtt_client_start()`, every announce is built in the mqtt task after it, so no lock.
- **Stored as `rb_cap` (u8 = 1) in the "ff" credential namespace.** `ff_store_sync_token()` erases "ff" on every token change, and a re-flash through our flasher always carries a new token and a new bootloader, so a reading cannot outlive the bootloader it was taken on. An OTA keeps it; a tokenless config never erases it; `ff_store_load()` probes `mqtt_pass`, so `rb_cap` alone does not fake a credential. `ff_identity_note_rollback_capable()` writes only when the cached value is not already true (no NVS write on every OTA boot); a failed save keeps `true` for this boot (the observation is real) and retries at the next PENDING_VERIFY boot. `ff_store_save_rollback_capable(void)` takes no argument: 0.4.7 can only store `true`. Known gap, already accepted in board-profiles.md: a flash that rewrites the bootloader without a new token (Arduino IDE upload, a future "keep identity" flash).
- **NEW-at-target ends `confirmed` with the detail `the bootloader never armed rollback for this image`.** `classify_txn` used to drop it as "stale transaction record … discarded", which parked the deploy at `rebooting` for good. Now it is `TXN_BOOTED_NEW`: nothing on connect (no `confirming`, which would promise a rollback deadline that does not exist), and at the announce ack, after the byte-identical `confirm_this_image() && s_txn.kind == TXN_CONFIRMING` branch and behind `if (s_rollback_decided)`, `accept_unverified_image()` re-reads the state and marks valid only from NEW; `confirmed` is queued only on ESP_OK (IDF 5.5 moves any non-VALID running entry to VALID). A reconnect before the PUBACK repeats the detail; a *reset* between mark-valid and the PUBACK re-reports `confirmed` without it (VALID carries no memory of NEW), which is accepted. **Why `confirmed` and not `failed`:** the image applied and is running, so `fw_version` shows the target; `failed` would contradict the announce and name a failure that did not happen, and the agent's status vocabulary is closed. What is missing is the board's safety net, a property of the board (`rollback_capable: false`), not of this deploy. **Not reachable in QEMU or on the bench S3** (our bootloader always arms rollback; forging otadata is forbidden): proven by `tests/test_agent_board_measurements.py` and the review only. The bench run belongs to R2b-test-5.
- **`false` is never emitted** (DECISIONS 2026-10-04 A3). There is no `cJSON_AddFalseToObject`/`AddBool` for the key, a test pins it, and the NEW-at-target branch logs the evidence R2b-test-5 will read (`… rollback_capable would be false, which this agent does not report until it is benched`) without storing or announcing anything.
- **Agent 0.4.7.** `spi_flash` added to the component's REQUIRES. Flash budgets raised to the measured bytes for all four targets. The announce measured 465 B in QEMU (ethernet, `ssid` null); a 32-byte SSID adds ≈ 40 B, well under esp-mqtt's 1024 B default.
- **Patch B is owed as the owner's `spec:` commit right after this one (amends 2026-10-04 A4).** A4 asks for Patch B "only in R2b-fw-2's commit". Its reason is order: Patch B alone turns `test_the_firmware_builds_exactly_the_spec_keys` red. Both the implement and the review harness forbid edits under `spec/`, so "same commit" cannot be met. The invariant that matters is "never before the agent emits the keys", and a `spec:` commit after this one keeps it, as 8cb5335 did for Patch A. The test is green with and without Patch B (proved in memory, see board-profiles.md → R2b-fw-2). The three lines go after `"ota_slot_size": 1966080,` in the `up/announce` example: `"flash_chip_size": 4194304,`, `"partition_table_sha256": "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed",`, `"rollback_capable": true,`. Nothing else under `spec/` changes.
- **Release note.** Prod's board learns `true` on its first 0.4.7 OTA, even from 0.3.1. That deploy itself still parks at `rebooting` (0.3.1 wrote no record, so there is no outcome to report; pre-existing). The ingestor already maps migration 0006, so no new deploy-order hazard.
- **Rejected:** observing only in `TXN_CONFIRMING` (prod's first OTA would learn nothing); `ff_txn` (erased by every `ff_txn_save()`) or a new namespace (outlives a re-flash); `failed` or a new status for NEW-at-target; announcing `false` before R2b-test-5; an `esp_flash_get_size()` fallback (the image header's claim); the partition array on the stack; deriving anything from `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`.

Amends 2026-10-04 (R2b-spec-2) A4: "same commit" becomes "never before".

---

## 2026-10-05 — The update flow is played end to end in a real Chromium against simulated boards; a `-rbtest` version is the broken build (R2b-test-3)

**Decided: `just update-e2e` (`frontend/scripts/update-flow-e2e.mjs`, Playwright resolved from `PLAYWRIGHT_MODULE`, not a dependency) drives Flow 2 through the dashboard against the dev stack with two simulator boards, and the simulator gets `--broken-marker=TEXT` (an image whose version contains TEXT never confirms, every other image follows `--confirm`).**

- **Simulator, not QEMU.** QEMU's confirm timer is 300 s, needs ESP-IDF builds, and the on-device half is already proven by R2-test-1 and R2b-test-2. What this task lacked was the dashboard and server half (CUJ-1 steps 5 and 6, and the wrong-layout refusal). The cost: the CUJ judge's "fw_version from the running image's own descriptor" is only met on QEMU/bench. `bench-judge confirmed` grades the good deploy on simulator rows; `rbtest` cannot pass on them (wording, window) and is not used.
- **`--broken-marker`, not two processes.** `--confirm never` is board-wide and per process, and the simulator does not persist `fw_version` across restarts, so good-then-broken on one board would need a second board or a restart that fakes "comes back on the version that worked". The marker is a substring (no pattern pitfalls); empty is refused (`"" in v` is always true). It must be written `--broken-marker=-rbtest`: argparse reads a bare `-rbtest` as an option.
- **An Arduino-layout simulator board.** The upload form's layout select offers only `ab-4m-v1` plus layouts the fleet reports, so without one the operator cannot pick the wrong layout; it is also the positive control (the same build is deployable to the board it fits).
- **Distinct bytes per label.** `artifacts` is keyed by sha256 with `ON CONFLICT DO NOTHING`, so the same bytes under a second label with another layout keep the FIRST layout silently. Each build is the real app-head fixture with its label written into the descriptor, which also makes the header read pre-fill the version. Labels carry a per-run id (labels are permanent). Not changed here.
- **Wait on the transaction, not on "a card".** The harness waits for this `cmd_id` to be terminal on the API and for a card whose text names this transaction's version, because the previous good card is still on screen when the next deploy starts. Screenshots are of the scenario's element (the dev fleet is hundreds of rows).
- **Vacuity switch** `E2E_BREAK=<scenario id>`; both runs of it fail as they must.
- **Spec proposal (not applied; `spec/` is protected):** in `spec/cujs.md` CUJ-1 Driver table, name `just update-e2e` for row 5 (dashboard and server half), row 6 (server and dashboard half; the on-device half stays QEMU/bench) and the wrong-layout row.
- **Rejected:** a second board for the broken build; restarting the simulator between deploys; QEMU for this task; adding Playwright as a dependency; full-page screenshots; changing the upload or pre-check code (nothing needed it).

Supersedes nothing.

---

## 2026-10-05 — The board measurements are stored as last reported, written as a triple on every announce; malformed is null, never a lost announce (R2b-be-6)

**Decided: `devices.flash_chip_size BIGINT NULL`, `devices.partition_table_sha256 TEXT NULL`, `devices.rollback_capable BOOLEAN NULL` (migration `0006`), right after `ota_slot_size` in spec key order. Both edges normalise through `src/fleetforge/announce_fields.py`, the never-raising module from R2b-be-5. `DeviceSummary` exposes all three after `ota_slot_size`.**

- **Schema.** BIGINT, not INTEGER, for `flash_chip_size`: the agent reads it with `esp_flash_get_physical_size(…, uint32_t*)`, and 2^31..2^32-1 overflows int4. No server default (no row is rewritten) and no CHECK: both edges normalise first, and a CHECK would turn a normaliser bug into an `IntegrityError` that loses the announce, `fw_version` included. `downgrade()` drops only device-reported data, which the next announce re-supplies.
- **A triple on every announce (the second exception to "absent means no change").** `store.py::_measurement_values` always writes all three, so a key that is absent or null writes NULL. The spec says "an absent field means unknown". The announce is the full identity, republished on every connect, and the retained copy is always the latest. An OTA back to an agent that does not send the keys must read as unknown, not repeat the old reading. It is also **fail-open** for R2b-be-7: NULL never refuses and never warns, while a stale value could raise a false `partition_table_mismatch` or `rollback_incapable`. Enroll follows the same rule through `IDENTITY_FIELDS`: a re-flash may have changed the bootloader and the table.
- **Malformed rules** (R2b-spec-2 / `board-profiles.md` → *Step 1 wire proposal*). `flash_chip_size`: an `int` that is not a `bool`, in `1..FLASH_CHIP_SIZE_MAX = 0xFFFF_FFFF`. Floats and strings are not coerced, and there is no power-of-two rule. The ceiling is load-bearing: past int64 (`2**63`, `18446744073709551616`), asyncpg raises inside the transaction and the announce is lost. `partition_table_sha256`: a `str` that `fullmatch`es `[0-9a-f]{64}`. It is never lowercased, because uppercase is malformed by definition. The class is explicit because `\d` accepts Unicode digits and `re.match(...$)` accepts a trailing newline; it also keeps out NUL and lone surrogates. `""` is malformed and logs. `rollback_capable`: `isinstance(value, bool)` only. `1`, `"true"` and `"false"` are NULL, and the before-validator returns a real bool, so pydantic's lax coercion has nothing left to do.
- **Logging.** The existing `_unusable` line: `device <id|?> announced an unusable <field> (<reason>); stored as null`, at INFO, never the value.
- **Edges.** `mode="before"` validators on `AnnouncePayload` and `EnrollRequest`, declared after `device_id` so `_logged_device_id` can name the board. A 422 on enroll would come after the token was read and is forbidden by the spec. `api/routers/enroll.py` is unchanged: adding the names to `IDENTITY_FIELDS` and the fields to `EnrollRequest` together is enough (a name in one but not the other would raise `AttributeError` after validation).
- **Read model / frontend hand-off.** The `DeviceSummary` fields default to `None` only so that no other constructor breaks. `_device_summary` always sets them, and `deploy` stays last. NULL is unknown, which includes `rollback_capable` on every board before its first OTA, so a dashboard may say "rollback unverified". **`frontend/src/api.ts` is unchanged on purpose** (the be-5 precedent). A later frontend task adds `flash_chip_size: number | null`, `partition_table_sha256: string | null` and `rollback_capable: boolean | null` after `ota_slot_size`. Until then the client ignores the extra keys.
- **Simulator.** `DeviceIdentity` always announces the three, null when unset, between `ota_slot_size` and `capabilities`. `--flash-chip-size`, `--partition-sha` (sent verbatim) and `--rollback-capable {true,false}` exist. No flags means all three null, which is what every agent ≤ 0.4.6 sends, so existing CUJs behave as before. There is no `validate()` rule.
- **No event change.** `device.announce` already prompts a re-read.
- **Test isolation fix.** `tests/test_api_devices.py::fleet` now also wipes devices on entry. `test_enroll.py` commits devices it never deletes, so `test_an_empty_fleet_is_an_envelope_not_an_error` failed whenever `test_enroll.py` ran first (as in this task's T1 command). That was pre-existing and is test-only. `test_a_requested_rows_sha256_appears_nowhere_in_the_steps` now asserts the quoted key `"sha256"`, since `partition_table_sha256` is a legitimate column name.
- **Deploy-order gotcha (release), as R2b-be-5.** The ingestor's ORM maps the new columns, so a new-image ingestor on a `0005` schema fails every message (`UndefinedColumnError`), the retained replay included. `services/scripts/deploy.sh` recreates the ingestor before `docker rollout fleetforge-api` migrates. At release, restart `fleetforge-ingestor` after the api rollout. On dev, restart `api` first (it migrates) and then `ingestor`.
- **Rejected:** adding the fields to `ANNOUNCE_FIELDS` (it skips `None`, which leaves stale values); an INTEGER column; lowercasing the sha; coercing `"true"` / `1`; a sim default sha (it would go stale under `--partition-layout ab-4m-arduino-v1`, and be-7 would refuse it); DB CHECKs; applying Patch B here (it goes in only with R2b-fw-2, DECISIONS 2026-10-04 A4); changing `api.ts` in a backend task.
- **Observed, not fixed:** `ota_slot_size` has `gt=0` on enroll but no upper bound in `AnnouncePayload`, so an announce with `ota_slot_size` past int64 would overflow asyncpg and lose the announce. This is the same class of bug the flash ceiling prevents, and a candidate Sprint 0 task.

Supersedes nothing.

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
