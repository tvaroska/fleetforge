"""HTTP Range artifact fetching, non-app cargo execution, and USB-C dock staging (`CUJ-3`).

Separated from `stage_runner.py` to keep each simulator module compact while maintaining
strict server-import independence (`tests/test_simulator.py`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.request
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

import aiomqtt

from fleetforge.simulator.board_state import (
    _SHA256_HEX,
    ANNOUNCE,
    CONFIRM_AUTO,
    DOWNLOAD_CHUNK_BYTES,
    DOWNLOAD_TIMEOUT_S,
    RESET_REASON_SW,
    RETURNED_TO,
    SELFTEST_FAIL_REASON,
    SELFTEST_FAILED,
    SELFTEST_PASSED,
    SELFTEST_TIMED_OUT,
    SLOT_LABELS,
    STATE_APPLYING,
    STATE_CONFIRMED,
    STATE_CONFIRMING,
    STATE_DOWNLOADING,
    STATE_FAILED,
    STATE_ROLLED_BACK,
    STATE_ROLLING_BACK,
    STATE_STAGED,
    STATE_STAGING,
    STATE_VERIFYING,
    PendingConfirm,
    Step,
    _is_valid_artifact_sig,
    _paced_publish,
    _silent,
    _verify_artifact_sig,
    encode,
    redact_url,
    up_topic,
)

if TYPE_CHECKING:
    from fleetforge.simulator.stage_runner import StageRunner


def _fetch(
    url: str,
    *,
    max_retries: int = 3,
    initial_backoff_s: float = 0.005,
    step: Step = _silent,
) -> bytes:
    """GET `url` into memory, chunked, with HTTP `Range` resume on interruption (`R6-FW-1`).

    When `offset == 0`, `url` is passed as a bare `str` to `urlopen` so test monkeypatches
    keep seeing the plain URL string; mid-stream retries send `Range: bytes=<offset>-`.
    """
    buffer = bytearray()
    attempt = 0
    while True:
        offset = len(buffer)
        request_or_url: str | urllib.request.Request = (
            urllib.request.Request(url, headers={"Range": f"bytes={offset}-"})  # noqa: S310
            if offset > 0
            else url
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request_or_url, timeout=DOWNLOAD_TIMEOUT_S
            ) as response:
                status_code = getattr(response, "status", None)
                if offset > 0 and status_code == 200:
                    buffer.clear()
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    buffer.extend(chunk)
            return bytes(buffer)
        except OSError:
            attempt += 1
            if attempt > max_retries:
                raise
            delay_s = initial_backoff_s * (1 << (attempt - 1))
            step(
                f"download retry #{attempt}/{max_retries} from offset {len(buffer)} "
                f"after {delay_s:.3f}s"
            )
            if delay_s > 0:
                time.sleep(delay_s)


def run_dock_stage(
    runner: StageRunner,
    cmd_id: str,
    data: bytes,
    *,
    sha256: str,
    version: str | None = None,
    sig: str | None = None,
    artifact_type: str = "app",
    size: int | None = None,
) -> list[dict[str, object]]:
    """`ff_ota_dock_stage`: stage a signed `.bin` or `data_partition` cargo over USB-C dock."""
    emitted: list[dict[str, object]] = []

    def emit(state: str, *, pct: int | None = None, detail: str | None = None) -> dict[str, object]:
        item = runner._record_dock_status(cmd_id, state, pct=pct, detail=detail)
        emitted.append(item)
        return item

    runner.identity = replace(runner.identity, link_type="dock", ssid=None)
    if not _SHA256_HEX.fullmatch(sha256):
        emit(STATE_FAILED, detail="artifact sha256 malformed")
        return emitted
    if sig is not None and not _is_valid_artifact_sig(sig):
        emit(STATE_FAILED, detail="artifact sig malformed")
        return emitted

    emit(STATE_STAGING)
    if size is not None and size != 0 and len(data) != size:
        emit(STATE_FAILED, detail="size mismatch")
        return emitted

    if artifact_type != "app":
        handler = runner.cargo_handlers.get(artifact_type)
        if handler is None and artifact_type not in runner.identity.capabilities:
            emit(STATE_FAILED, detail="unsupported artifact type")
            return emitted
        emit(STATE_DOWNLOADING)
        runner.downloads += 1
        emit(STATE_VERIFYING)
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha256:
            emit(STATE_FAILED, detail="sha256 mismatch")
            return emitted
        if sig is not None:
            if not _verify_artifact_sig(digest, sig, runner.signing_pubkey_ring):
                emit(STATE_FAILED, detail="signature invalid")
                return emitted
        elif runner.require_signature:
            emit(STATE_FAILED, detail="signature missing")
            return emitted
        emit(STATE_STAGED, pct=100)
        emit(STATE_APPLYING, pct=100)
        if handler is not None:
            try:
                handler(data, version)
            except Exception as exc:  # noqa: BLE001
                emit(STATE_FAILED, detail=f"cargo apply failed: {type(exc).__name__}")
                return emitted
        if version:
            updated_subs = dict(runner.identity.sub_versions or {})
            updated_subs[artifact_type] = version
            runner.identity = replace(runner.identity, sub_versions=updated_subs)
        emit(STATE_CONFIRMING)
        emit(STATE_CONFIRMED, pct=100)
        return emitted

    if runner.pending is not None:
        emit(STATE_FAILED, detail="the running image is not confirmed yet")
        return emitted
    if runner.staged is not None:
        emit(STATE_FAILED, detail="an update is already staged and waits for a reboot")
        return emitted

    effective_size = size if size is not None else len(data)
    if effective_size > runner.identity.ota_slot_size or len(data) > runner.identity.ota_slot_size:
        emit(STATE_FAILED, detail="artifact larger than the ota slot")
        return emitted

    emit(STATE_DOWNLOADING)
    runner.downloads += 1
    emit(STATE_VERIFYING)
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        emit(STATE_FAILED, detail="sha256 mismatch")
        return emitted
    if sig is not None:
        if not _verify_artifact_sig(digest, sig, runner.signing_pubkey_ring):
            emit(STATE_FAILED, detail="signature invalid")
            return emitted
    elif runner.require_signature:
        emit(STATE_FAILED, detail="signature missing")
        return emitted

    emit(STATE_STAGED, pct=100)
    runner.staged = cmd_id
    runner.staged_version = version if isinstance(version, str) and version else None
    return emitted


def run_dock_reboot_staged(runner: StageRunner) -> None:
    """Caller-triggered reboot into a dock-staged image (paired with `ff_ota_dock_stage`)."""
    if runner.staged is None:
        return
    cmd_id = runner.staged
    staged_version = runner.staged_version
    runner._clear_staged()
    runner.pending = PendingConfirm(
        cmd_id=cmd_id, previous_identity=runner.identity, previous_slot=runner.slot
    )
    runner.slot = SLOT_LABELS[1] if runner.slot == SLOT_LABELS[0] else SLOT_LABELS[0]
    runner._confirm_deadline = None
    runner.identity = replace(runner.identity, reset_reason=RESET_REASON_SW)
    if staged_version:
        runner.identity = replace(runner.identity, fw_version=staged_version)
    runner.reboot.set()


def run_dock_confirm_boot(runner: StageRunner) -> list[dict[str, object]]:
    """`ff_mqtt_confirm_offline`: evaluate `PENDING_VERIFY` locally in Venue Offline Mode."""
    emitted: list[dict[str, object]] = []

    def emit(cmd_id: str, state: str, *, pct: int | None = None, detail: str | None = None) -> None:
        item = runner._record_dock_status(cmd_id, state, pct=pct, detail=detail)
        emitted.append(item)

    runner.reboot.clear()
    if runner.rolled_back is not None:
        head = runner.rolled_back_head
        emit(
            runner.rolled_back,
            STATE_ROLLED_BACK,
            detail=(
                f"{head}{RETURNED_TO}{runner.slot}"
                if head is not None
                else f"returned to {runner.identity.fw_version}; the new image did not confirm"
            ),
        )
        runner.rolled_back = None
        runner.rolled_back_head = None
        return emitted

    pending = runner.pending
    if pending is None:
        return emitted

    emit(pending.cmd_id, STATE_CONFIRMING)
    broken = runner._is_broken(runner.identity.fw_version)
    registered = runner._selftest_registered
    if runner.confirm == CONFIRM_AUTO and not broken and registered:
        if runner._fails_selftest(runner.identity.fw_version):
            head = SELFTEST_FAILED + SELFTEST_FAIL_REASON
            emit(pending.cmd_id, STATE_ROLLING_BACK)
            runner.identity = replace(pending.previous_identity, reset_reason=RESET_REASON_SW)
            runner.slot = pending.previous_slot
            runner.pending = None
            runner.returnable = None
            runner._confirm_deadline = None
            emit(
                pending.cmd_id,
                STATE_ROLLED_BACK,
                detail=f"{head}{RETURNED_TO}{runner.slot}",
            )
            return emitted

        emit(
            pending.cmd_id,
            STATE_CONFIRMED,
            pct=100,
            detail=SELFTEST_PASSED,
        )
        runner.returnable = (pending.previous_identity, pending.previous_slot)
        runner.pending = None
        runner._confirm_deadline = None
        return emitted

    head = SELFTEST_TIMED_OUT if registered else None
    emit(pending.cmd_id, STATE_ROLLING_BACK)
    runner.identity = replace(pending.previous_identity, reset_reason=RESET_REASON_SW)
    runner.slot = pending.previous_slot
    runner.pending = None
    runner.returnable = None
    runner._confirm_deadline = None
    emit(
        pending.cmd_id,
        STATE_ROLLED_BACK,
        detail=(
            f"{head}{RETURNED_TO}{runner.slot}"
            if head is not None
            else f"returned to {runner.identity.fw_version}; the new image did not confirm"
        ),
    )
    return emitted


def run_dock_drain(runner: StageRunner) -> dict[str, object]:
    """`ff_log_drain_json` + `ff_telemetry_drain_json`: drain rings for USB-C dock sync."""
    now = runner.clock()
    announce = replace(runner.identity, link_type="dock", ssid=None).announce()
    statuses = list(runner._dock_statuses)
    runner._dock_statuses.clear()

    telemetry_batches: list[dict[str, object]] = []
    while True:
        built = runner._offline_telemetry.next_payload()
        if built is None:
            break
        payload_bytes, last_seq, dropped = built
        runner._offline_telemetry.commit(last_seq, dropped)
        telemetry_batches.append(json.loads(payload_bytes.decode("utf-8")))

    log_batches: list[dict[str, object]] = []
    if runner._offline_logs or runner._offline_logs_dropped > 0:
        lines = [
            {
                "age_ms": max(int((now - taken) * 1000), 0),
                "level": level,
                "text": text,
            }
            for taken, level, text in runner._offline_logs
        ]
        log_batches.append({"dropped": runner._offline_logs_dropped, "lines": lines})
        runner._offline_logs.clear()
        runner._offline_logs_dropped = 0

    return {
        "announce": announce,
        "statuses": statuses,
        "telemetry": telemetry_batches,
        "logs": log_batches,
    }


async def run_stage_cargo(
    runner: StageRunner,
    client: aiomqtt.Client,
    cmd_id: str,
    *,
    artifact_type: str,
    url: str,
    expected_sha: str,
    sig: str | None,
    version: str | None,
    step: Step,
    fetch_fn: Callable[..., bytes],
) -> None:
    """`R7-cargo-1`: Execute a non-`app` companion cargo update without rebooting."""
    handler = runner.cargo_handlers.get(artifact_type)
    if handler is None and artifact_type not in runner.identity.capabilities:
        await runner._status(client, cmd_id, STATE_FAILED, step, detail="unsupported artifact type")
        return

    await runner._status(client, cmd_id, STATE_DOWNLOADING, step)
    try:
        data = await asyncio.to_thread(
            fetch_fn,
            url,
            max_retries=runner.download_retries,
            initial_backoff_s=runner.download_backoff_s,
            step=step,
        )
    except OSError as exc:
        await runner._status(
            client, cmd_id, STATE_FAILED, step, detail=f"download failed: {type(exc).__name__}"
        )
        return
    runner.downloads += 1
    step(f"download {len(data)} bytes from {redact_url(url)} (download #{runner.downloads})")

    await runner._status(client, cmd_id, STATE_VERIFYING, step)
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha:
        await runner._status(client, cmd_id, STATE_FAILED, step, detail="sha256 mismatch")
        return
    if sig is not None:
        if not _verify_artifact_sig(digest, sig, runner.signing_pubkey_ring):
            await runner._status(client, cmd_id, STATE_FAILED, step, detail="signature invalid")
            return
    elif runner.require_signature:
        await runner._status(client, cmd_id, STATE_FAILED, step, detail="signature missing")
        return

    await runner._status(client, cmd_id, STATE_STAGED, step, pct=100)
    await runner._status(client, cmd_id, STATE_APPLYING, step, pct=100)
    if handler is not None:
        try:
            handler(data, version)
        except Exception as exc:  # noqa: BLE001
            await runner._status(
                client,
                cmd_id,
                STATE_FAILED,
                step,
                detail=f"cargo apply failed: {type(exc).__name__}",
            )
            return

    if version:
        updated_subs = dict(runner.identity.sub_versions or {})
        updated_subs[artifact_type] = version
        runner.identity = replace(runner.identity, sub_versions=updated_subs)
        await _paced_publish(
            client,
            runner.link,
            up_topic(runner.identity.device_id, ANNOUNCE),
            encode(runner.identity.announce()),
            retain=True,
            step=step,
        )

    await runner._status(client, cmd_id, STATE_CONFIRMING, step)
    await runner._status(client, cmd_id, STATE_CONFIRMED, step, pct=100)
