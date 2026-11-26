# Critical Paths

Registry of protected/sensitive paths. Any change touching a path here requires stronger review before commit; `spec/` changes are proposed and explicitly approved rather than edited ad-hoc.

Some Fleetforge mistakes cannot recover via OTA: a board's partition table, bootloader flags, and wire protocol version are flash-time immutables whose breakage requires physical recall.

| Path | Why it is critical |
| :--- | :--- |
| `spec/device-protocol.md` | **Near-frozen** wire contract: topic tree, QoS/retain matrix, payload schemas. Additive only; breaking changes recall deployed R0 boards. |
| `spec/prd.md` | Authoritative targets, timing, and retention numbers referenced across agent, ingestor, and dashboard. |
| `agent/partitions.csv`, `agent/sdkconfig.defaults` | **Flash-time immutables** (`ab-4m-v1`, `ota_slot_size=1966080`, rollback enabled). Never enable eFuse-burning options (`agent/tools/verify_bundle.py`). |
| `agent/components/fleetforge/examples/Basic/partitions.csv`, `agent/components/fleetforge/examples/basic_idf/partitions.csv`, `agent/components/fleetforge/examples/basic_idf/sdkconfig.defaults` | **Flash-time immutables** (`ab-4m-arduino-v1` and IDF `ab-4m-v1`) carried by library consumer builds; new map = new layout id, never edit in place. |
| `agent/components/fleetforge/examples/basic_idf/main/fleetforge_start.c`, `agent/components/fleetforge/src/Fleetforge.cpp`, `scripts/arduino_package.py` | Confirm-timer boot ladder (`verifyRollbackLater()` + `ff_mqtt_arm_confirm_timer()`) and Arduino IDE packaging parity. |
| `agent/components/fleetforge/src/ff_ota.c`, `agent/components/fleetforge/src/ff_mqtt.c` | A/B slot apply, confirm-timer rollback, ECDSA P-256 signature verification, and production key checks; bugs brick devices or bypass signing. |
| `mosquitto/acl`, `mosquitto/bootstrap.sh`, `mosquitto/configure.sh` | Two `%u` ACL patterns are the entire fleet MQTT authz; `bootstrap.sh` (offline init) and `configure.sh` (live `$CONTROL`) must stay separate. |
| `src/fleetforge/auth/firebase.py`, `src/fleetforge/api/routers/auth.py`, `src/fleetforge/api/routers/users.py`, `src/fleetforge/api/routers/projects.py`, `src/fleetforge/api/routers/groups.py`, `src/fleetforge/api/deps.py` | Admin login, cookie flags, Firebase ID tokens, scoped/pinned `ffa_` tokens, and `check_project_access` / `authorize_artifact_digests` isolation. |
| `src/fleetforge/api/routers/enrollment.py`, `src/fleetforge/api/routers/enroll.py` | Enrollment token issuance and atomic single-use burn; failure lets arbitrary devices join a fleet. |
| `src/fleetforge/signing.py`, `src/fleetforge/artifact_urls.py`, `src/fleetforge/deploy_urls.py`, `src/fleetforge/api/routers/deploys.py` | Artifact ECDSA P-256 verification and the sole HMAC download URL-minting policy (`deploy_urls.py::mint_deploy_artifact_url`). |
| `src/fleetforge/secrets_crypto.py`, `src/fleetforge/api/routers/project_secrets.py`, `src/fleetforge/api/routers/device_secrets.py` | Envelope encryption of rotating `project_secrets` and per-device HTTPS Basic secret pull (`SECRETS_ENCRYPTION_KEY`). |
| `src/fleetforge/builds.py`, `src/fleetforge/build_adapters.py`, `src/fleetforge/simulation.py`, `src/fleetforge/url_safety.py`, `src/fleetforge/api/routers/swarm.py` | Sandboxed ESP-IDF container build execution, QEMU pre-flight runner, outbound SSRF / `git_ref` / URL safety guards, and gateway Basic auth + delta patching. |
| `alembic/versions/` | Irreversible database schema and data migrations. |
| `.env.example` | Environment template; never commit real credentials, keys, or secrets. |
