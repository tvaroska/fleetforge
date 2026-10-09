# Critical Paths

Registry of protected/sensitive paths. Any `/implement` task touching a path here
**auto-escalates**: stronger model + mandatory review before commit. `spec/` has protection — agents PROPOSE changes to spec, they do not edit it during `/implement`.

Fleetforge has an unusual property for a software project: **some mistakes cannot recover through shipping new software**. An agent flashed onto a board carries a partition
table, a bootloader and a protocol version that no OTA can change. Getting those wrong
means physically getting every deployed device — the exact intervention this product
exists to delete. The first two rows below are that class of mistake.

| Path | Why it is critical |
|------|-------------------|
| `spec/device-protocol.md` | **Near-frozen**. An R0 agent speaks this protocol until someone physically gets the board. Additive change is cheap. Anything else is a recall. Topic tree, QoS/retain semantics and payload schemas are all load-bearing. |
| `agent/partitions.csv`, `agent/sdkconfig.defaults`, `agent/sdkconfig.defaults.<target>` (partition table, bootloader options, eFuse burns) | **Flash-time immutables** — not changeable by OTA. Wrong at R0 = physical recall of the fleet. See `design/architecture.md` → *Flash-time immutables*. `ab-4m-v1` is a three-way contract with `spec/device-protocol.md` (`ota_slot_size` 1966080) and `tests/test_agent_partitions.py`. A new layout is a new id, never an edit to this one. Anti-rollback / secure boot / flash encryption burn eFuses per board — `agent/tools/verify_bundle.py` fails the build if any is ever enabled in the resolved config. |
| `agent/components/fleetforge/examples/*/partitions.csv` (`ab-4m-arduino-v1`, the Arduino library's sketch-local table) | **Flash-time immutable**, exactly like `agent/partitions.csv`: every board flashed from a library sketch carries it for life. Three-way contract with `spec/device-protocol.md` (fingerprint `05528998…1fc4`, `ota_slot_size` 1966080) and `tests/test_arduino_library.py`. A different map is a new layout id, never an edit. |
| `agent/components/fleetforge/src/Fleetforge.cpp` (the Arduino library's rollback posture) | The library build's half of the confirm-timer row below: the strong `verifyRollbackLater()` returning true (without it Arduino's `initArduino()` confirms every OTA'd image before `setup()`), the confirm timer armed first in `begin()`, and the `#error` on `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`. Pinned by `tests/test_arduino_library.py`. |
| `spec/` | THE WHAT — requirements, targets, contracts. Status-free. Changes are proposals, reviewed. |
| `spec/prd.md` → *Requirements & targets* | Downstream docs and code resolve against this table. Changing a number here silently changes behavior in the agent, the ingestor and the dashboard. |
| Device-side confirm timer / rollback path (agent firmware) | The whole bricking gamble. A bug here means a board that cannot recover itself — the one failure the product must never have. |
| A/B slot apply logic (agent firmware) | Writing the wrong slot, or a non-atomic switch, bricks the device. |
| Mosquitto ACL configuration (`mosquitto/acl`) / dynsec provisioning (`mosquitto/bootstrap.sh`, `mosquitto/configure.sh`, `dynamic-security.json`) | Two pattern rules in `mosquitto/acl` are the entire fleet authz — a wrong pattern lets any device impersonate any other. Dynsec is authentication only (it has no `%u`), so the `device` role must stay empty. `just broker-check` is the proof. The two scripts are two phases and must stay separate: `bootstrap.sh` only creates the store before the broker starts, `configure.sh` applies every role and client to the RUNNING broker over `$CONTROL`. Writing dynsec state to the file underneath a live broker is invisible to it and then erased by it (ops-log F-2026-09-23-002). |
| Enrollment token issuance & burn (`R0-be-2`, `R0-be-4`) | A token that fails to burn lets anyone with one board enroll arbitrary devices into the fleet. |
| Admin auth (token table, login, cookie flags) | Single admin credential on a public-facing API. Bypass = full fleet control. |
| Artifact signing keys & `signed_url` generation | Signature *is* the authorization for artifact download. Signing keys are what make R6's verification meaningful. |
| Alembic migrations (`alembic/versions/`) | Irreversible schema/data changes. |
| Traefik entrypoints / TCP router (in the `services` repo) | Shared ingress. A change here affects every other app on the host, not just fleetforge. |
| Secrets / env (`.env*`, GCS service-account key, broker credentials) | Never commit real values. Prod env lives in `services/prod/.env` — confirm before changing. |
