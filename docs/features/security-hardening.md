# Signed OTA + Resumable Hardening (→ v1 complete)

**Priority:** P0
**Target:** R6 (→ v1 complete)
**Depends on:** OTA Deploy & Auto-Rollback (ota-deploy.md) — R2

## Overview

Make it production-grade: **app-level firmware signature verification** before apply,
**resumable downloads** with retry/backoff for flaky Wi-Fi, and both KPIs surfaced.

**App-level signing, not Secure Boot v2**. Secure Boot v2 burns a key digest to eFuse and
needs a re-signed bootloader. Thus, it can never work on an already-deployed board.
It is post-v1 and new-devices-only. What R6 ships is pure software the agent can receive
over OTA. The two are not interchangeable. See [design/architecture.md](../../design/architecture.md) →
*Flash-time immutables*. This closes v1: auto-rollback (per-device) + checked, signed, resumable transport.
The simulation pre-flight gate is V2 (R9), so v1's defense in depth is one layer deep
by design — see [releases.md](../releases.md).

## Phase 1: R6 — Signed OTA + resumable hardening

| ID | Task | Priority | Effort | Status |
|----|------|----------|--------|--------|
| R6-SEC-1 | Firmware signing: sign artifacts server-side. Agent checks signature before apply | P0 | 2d | Done (2026-10-22) |
| R6-SEC-2 | Signing-key management: generation, storage, rotation, docs + defaults | P1 | 1d | Done (2026-10-22) |
| R6-FW-1 | Resumable download (range/offset), retry + backoff | P0 | 1.5d | Done (2026-10-22) |
| R6-BE-1 | Surface both KPIs from the deploy-event history recorded since R1 | P0 | 1d | Done (2026-10-22) |
| R6-FE-1 | Dashboard KPI view (delivery success versus fleet safety, with the gap) | P0 | 1d | Done (2026-10-22) |
| R6-TEST-1 | E2E: unsigned/tampered artifact rejected. Interrupted download resumes | P0 | 1d | Done (2026-10-22) |

**Done when:** you can run it in earnest — production-grade safety + security. **v1 complete.**

## Implementation Details (Shipped 2026-10-22)

1. **Signature Format & Dual-Mode Signing (`R6-SEC-1`, `R6-SEC-2`):**
   - Detached signature format: `ecdsa-p256-sha256:<base64_der>` over the 32-byte raw SHA-256 digest of the `.bin` image (`spec/device-protocol.md` → `artifact.sig`).
   - **CI / Offline Detached Signing:** `python -m fleetforge.signing sign <app.bin> --key <signing-key.pem>` produces the `ecdsa-p256-sha256:...` string, which `POST /v1/artifact?sig=...` verifies against the server's trusted public-key rotation ring (`ARTIFACT_SIGNING_PUBLIC_KEYS`) before storing in `artifacts.sig` (migration `0012_artifact_sig.py`).
   - **Server-Side Auto-Signing:** When `ARTIFACT_SIGNING_KEY` is configured on the server, any upload without an explicit `?sig=` parameter is signed server-side automatically.
   - **Key Rotation Ring ($\ge 2$ keys):** `DEFAULT_PUBLIC_KEY_RING` (`src/fleetforge/signing.py`), `DEFAULT_SIGNING_PUBKEY_RING` (`src/fleetforge/simulator/device.py`), and `s_signing_pubkey_ring` (`agent/components/fleetforge/src/ff_ota.c`) carry both primary and secondary ECDSA P-256 SubjectPublicKeyInfo PEM keys so signing keys can rotate without stranding deployed boards.
   - **Device Verification Before Apply:** `ff_mqtt.c::on_stage` validates the `artifact.sig` grammar before starting OTA (`detail: "artifact sig malformed"`), and `ff_ota.c::verify_artifact_signature` verifies the ECDSA P-256 signature with `mbedtls_pk_verify` over the read-back flash SHA-256 digest before `esp_https_ota_finish()` ever moves the boot pointer (`detail: "signature invalid"` or `"signature missing"` when `FF_OTA_REQUIRE_SIGNATURE=1`).

2. **Resumable HTTP Range Downloads (`R6-FW-1`):**
   - `ff_ota.c` enables `.partial_http_download = true` with `OTA_RANGE_CHUNK_BYTES` (`65536`) and wraps transient mid-stream chunk errors in `resume_range_perform()` with exponential backoff (`1 s`, `2 s`, `4 s`) and `bytes=<offset>-` resume logging.
   - `simulator/device.py::_fetch` tracks the partial byte buffer across up to 3 retries (`1 s`, `2 s`, `4 s`) and sends `Range: bytes=<offset>-` to resume from the exact byte offset where the link dropped.

3. **Delivery Success & Fleet Safety KPIs (`R6-BE-1`, `R6-FE-1`):**
   - `deploy_kpis()` (`src/fleetforge/deploys.py`) and `GET /v1/devices/kpis` compute both V1 KPIs over `deploy_events` history (excluding `cancelled` and standalone `rollback` verb rows, and preserving decommissioned-device history):
     - **Delivery success ($\ge 90\%$):** `confirmed / (confirmed + rolled_back + failed)`
     - **Fleet safety ($= 100\%$):** `(confirmed + rolled_back) / (confirmed + rolled_back + failed)`
     - **Rollback saves gap:** `rolled_back` (`fleet_safety_pct - delivery_success_pct`)
   - `DeployKpiStrip` (`frontend/src/FleetView.tsx`, `data-testid="deploy-kpis"`) renders both KPI cards, target badges, and the rollback-saves gap at the top of the Fleet view.

## Metrics

Definitions and targets: [prd.md](../../spec/prd.md) → *Success criteria*. The gap between the
two numbers tells you *which stage* to fix.

**Recording starts at R1, not here**. The `deploy_events` table is in the R0 schema and
every deploy outcome is written from R1 onward — otherwise R6 arrives with two metrics
and no history to compute them from.

## Post-v1 hardening

Per-device mTLS certs (replace token-only trust from R0).
