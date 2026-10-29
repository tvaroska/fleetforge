# Runbook — Backup & Restore (PostgreSQL + Mosquitto Dynamic Security)

FleetForge's enrolled fleet state spans **two coupled stores**:

1. **PostgreSQL (`fleetforge` database):** `projects`, `devices` (including `mqtt_password_hash` for `GET /v1/device-secrets`), `enrollment_tokens`, `artifacts`, `artifact_versions`, `deploy_events`, and `project_secrets`.
2. **Mosquitto Persistence Volume (`/mosquitto/data/`):**
   - `dynamic-security.json` — the per-device PBKDF2-SHA512 MQTT credentials and role assignments (`device`, `ingestor`, `commander`) created by `POST /v1/enroll` over `$CONTROL/dynamic-security/v1`.
   - `mosquitto.db` — retained `up/announce`, `up/presence`, `up/status`, `dn/cfg` payloads and queued QoS 1 `dn/cmd` messages for sleepy boards.

> **Why both must be backed up together (`OPS-1`):** `POST /v1/enroll` returns the board's plaintext `mqtt_password` once to the device (where it lives in NVS `ff_id`) and hashes it into Mosquitto's `dynamic-security.json`. Restoring PostgreSQL without `dynamic-security.json` leaves every enrolled board in `devices` unable to authenticate to MQTT (`CONNACK 5: Not authorized`) until re-enrolled.

---

## 1. Atomic Backup (Production or Self-Hosted Stack)

Run the backup from the control host (or in cron alongside `pg_dump`):

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="/tmp/fleetforge-backup-${STAMP}"
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

# 1. Dump PostgreSQL (custom format, atomic snapshot)
ssh prod 'cd /opt/boris/prod && docker compose exec -T postgres sh -c '\''pg_dump -U "$POSTGRES_USER" -Fc fleetforge'\''' \
  > "${BACKUP_DIR}/fleetforge.dump"

# 2. Archive Mosquitto dynamic-security.json + mosquitto.db (root:mosquitto 0600 inside container)
ssh prod 'cd /opt/boris/prod && docker compose exec -T -u 0 mosquitto tar -C /mosquitto/data -czf - dynamic-security.json mosquitto.db' \
  > "${BACKUP_DIR}/mosquitto-data.tar.gz"

# 3. Verify both archives are non-empty
test -s "${BACKUP_DIR}/fleetforge.dump"
tar -tzf "${BACKUP_DIR}/mosquitto-data.tar.gz" | grep -q '^dynamic-security.json$'
```

When uploading to GCS (`gs://btvaroska/backup/`), keep the same timestamp prefix on both artifacts (`fleetforge-${STAMP}.dump` and `fleetforge-mosquitto-${STAMP}.tar.gz`).

---

## 2. Restore Procedure

1. **Stop `fleetforge-api` and `fleetforge-ingestor`** so no new enrollments or state transitions occur mid-restore:
   ```bash
   ssh prod 'cd /opt/boris/prod && docker compose stop fleetforge-api fleetforge-ingestor'
   ```
2. **Restore PostgreSQL (`fleetforge` database only):**
   ```bash
   ssh prod 'cd /opt/boris/prod && docker compose exec -T postgres sh -c '\''pg_restore -U "$POSTGRES_USER" --clean --if-exists -d fleetforge'\''' \
     < "${BACKUP_DIR}/fleetforge.dump"
   ```
3. **Restore Mosquitto `dynamic-security.json` and `mosquitto.db`:**
   Mosquitto holds `dynamic-security.json` in memory while running and overwrites it on `$CONTROL` mutations. Stop `mosquitto` before extracting the archive into `/mosquitto/data`:
   ```bash
   ssh prod 'cd /opt/boris/prod && docker compose stop mosquitto'
   ssh prod 'cd /opt/boris/prod && docker compose run --rm --no-deps -T -u 0 --entrypoint sh mosquitto -c "tar -C /mosquitto/data -xzf - && chown mosquitto:mosquitto /mosquitto/data/dynamic-security.json /mosquitto/data/mosquitto.db && chmod 600 /mosquitto/data/dynamic-security.json"' \
     < "${BACKUP_DIR}/mosquitto-data.tar.gz"
   ssh prod 'cd /opt/boris/prod && docker compose start mosquitto'
   ```
4. **Start `fleetforge-api` and `fleetforge-ingestor`, then run the release gate:**
   ```bash
   ssh prod 'cd /opt/boris/prod && docker compose up -d fleetforge-api fleetforge-ingestor'
   just deploy-check-prod
   ```

---

## 3. Reconciling Unprovisioned Devices (`broker_provisioned_at IS NULL`)

If a transient broker outage occurred during `POST /v1/enroll` (after the DB commit and before `$CONTROL` `createClient` completed), the enrollment returned `503 Retry-After: 5` and left `broker_provisioned_at IS NULL`.

To audit any devices whose DB row exists without confirmed broker provisioning:

```bash
ssh prod 'cd /opt/boris/prod && docker compose exec -T postgres sh -c '\''psql -U "$POSTGRES_USER" -d fleetforge -c "SELECT device_id, project_id, enrolled_at FROM devices WHERE broker_provisioned_at IS NULL AND decommissioned_at IS NULL ORDER BY enrolled_at;"'\'''
```

- If the board is still within `ENROLL_RETRY_WINDOW_S` (default `300s`), its automatic retry of `POST /v1/enroll` with the same token re-runs `provisioner.ensure_client` and stamps `broker_provisioned_at`.
- If the retry window has expired and the board never received the `200 OK` response, decommission the stale row (`DELETE /v1/devices/{device_id}`) and issue a fresh enrollment token.
