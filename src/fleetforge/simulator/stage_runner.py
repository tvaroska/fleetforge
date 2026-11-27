"""Update transaction state machine (`StageRunner`) for the simulated ESP32 board.

Implements `stage` and delegates R5 verb handling, safe-window release, and post-reboot
outcomes to `stage_verbs.py` and USB-C dock / companion-cargo staging to `dock_ops.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import aiomqtt

from fleetforge.simulator.board_state import (
    _SHA256_HEX,
    ACK,
    ACK_NOT_IMPLEMENTED,
    ACK_REFUSED,
    APPLY_ON_COMMAND,
    CAPABILITY_SELFTEST,
    COMMAND_STAGE,
    CONFIRM_AUTO,
    CONFIRM_MODES,
    DEFAULT_CONFIRM_TIMEOUT_S,
    DEFAULT_SIGNING_PUBKEY_RING,
    RESET_REASON_SW,
    SAFE_WINDOW_AUTO,
    SAFE_WINDOW_FILE_PREFIX,
    SAFE_WINDOW_HOLD,
    SLOT_LABELS,
    STATE_APPLYING,
    STATE_AWAITING_SAFE_WINDOW,
    STATE_DOWNLOADING,
    STATE_FAILED,
    STATE_REBOOTING,
    STATE_STAGED,
    STATE_STAGING,
    STATE_VERIFYING,
    STATUS,
    VERB_COMMANDS,
    DeviceIdentity,
    LinkProfile,
    PendingConfirm,
    Step,
    _is_valid_artifact_sig,
    _paced_publish,
    _verify_artifact_sig,
    check_safe_window,
    encode,
    redact_url,
    redacted_command,
    up_topic,
)
from fleetforge.simulator.dock_ops import (
    _fetch,
    run_dock_confirm_boot,
    run_dock_drain,
    run_dock_reboot_staged,
    run_dock_stage,
    run_stage_cargo,
)
from fleetforge.simulator.errors import SimulatorError
from fleetforge.simulator.stage_verbs import (
    run_apply,
    run_cancel,
    run_carry_out_rollback,
    run_confirm_timeout,
    run_deferred_rollback,
    run_on_boot,
    run_reboot_command,
    run_release_if_safe,
    run_rollback,
    run_rollback_command,
    run_verb,
)
from fleetforge.simulator.telemetry import TelemetryBuffer


def _resolve_fetch() -> Callable[..., bytes]:
    dev_mod = sys.modules.get("fleetforge.simulator.device")
    candidate = getattr(dev_mod, "_fetch", None)
    return candidate if callable(candidate) else _fetch


@dataclass(slots=True)
class StageRunner:
    """Executes `stage` commands and owns everything that outlives one MQTT session."""

    identity: DeviceIdentity
    link: LinkProfile
    safe_window: str = SAFE_WINDOW_AUTO
    confirm: str = CONFIRM_AUTO
    confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S
    broken_marker: str | None = None
    selftest_fail_marker: str | None = None
    require_signature: bool | None = None
    signing_pubkey_ring: tuple[str, ...] = DEFAULT_SIGNING_PUBKEY_RING
    download_retries: int = 3
    download_backoff_s: float = 0.005
    cargo_handlers: dict[str, Callable[[bytes, str | None], None]] = field(default_factory=dict)
    offline_mode: bool = False
    clock: Callable[[], float] = time.monotonic
    seen: set[str] = field(default_factory=set)
    downloads: int = 0
    reboot: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    pending: PendingConfirm | None = field(default=None, init=False)
    rolled_back: str | None = field(default=None, init=False)
    rolled_back_head: str | None = field(default=None, init=False)
    slot: str = field(default=SLOT_LABELS[0], init=False)
    staged: str | None = field(default=None, init=False)
    staged_version: str | None = field(default=None, init=False)
    apply_deferred: str | None = field(default=None, init=False)
    rollback_deferred: str | None = field(default=None, init=False)
    reboot_deferred: bool = field(default=False, init=False)
    returnable: tuple[DeviceIdentity, str] | None = field(default=None, init=False)
    last_reboot_cmd_id: str | None = field(default=None, init=False)
    _confirm_deadline: float | None = field(default=None, init=False, repr=False)
    _dock_statuses: list[dict[str, object]] = field(default_factory=list, init=False, repr=False)
    _offline_logs: list[tuple[float, str, str]] = field(
        default_factory=list, init=False, repr=False
    )
    _offline_logs_dropped: int = field(default=0, init=False, repr=False)
    _offline_telemetry: TelemetryBuffer = field(init=False, repr=False)

    def __post_init__(self) -> None:
        check_safe_window(self.safe_window)
        if self.confirm not in CONFIRM_MODES:
            raise SimulatorError(
                f"--confirm must be one of {list(CONFIRM_MODES)}, not {self.confirm!r}"
            )
        if not self.confirm_timeout_s > 0:
            raise SimulatorError(
                f"--confirm-timeout must be a positive number of seconds, "
                f"not {self.confirm_timeout_s!r}"
            )
        if self.broken_marker == "":
            raise SimulatorError("--broken-marker must not be empty")
        if self.selftest_fail_marker == "":
            raise SimulatorError("--selftest-fail-marker must not be empty")
        if self.selftest_fail_marker is not None and not self._selftest_registered:
            raise SimulatorError(
                "an image can fail a self-test only if one is registered: "
                f"add --capabilities {CAPABILITY_SELFTEST}"
            )
        if self.require_signature is None:
            self.require_signature = "sig" in self.identity.capabilities
        self._offline_telemetry = TelemetryBuffer(clock=self.clock)

    def set_offline_mode(self, offline: bool) -> None:
        self.offline_mode = bool(offline)

    def record_log(
        self, level: str, text: str, *, prev_boot: bool = False, capacity: int = 32
    ) -> None:
        rendered = f"[prev-boot] {text}" if prev_boot else text
        if len(self._offline_logs) >= capacity:
            self._offline_logs.pop(0)
            self._offline_logs_dropped += 1
        self._offline_logs.append((self.clock(), level, rendered))

    def record_telemetry(self, name: str, value: float) -> bool:
        return self._offline_telemetry.record(name, value)

    def _record_dock_status(
        self,
        cmd_id: str,
        state: str,
        *,
        pct: int | None = None,
        detail: str | None = None,
    ) -> dict[str, object]:
        status_item: dict[str, object] = {
            "cmd_id": cmd_id,
            "state": state,
            "pct": pct,
            "detail": detail,
        }
        self._dock_statuses.append(status_item)
        return status_item

    def dock_stage(
        self,
        cmd_id: str,
        data: bytes,
        *,
        sha256: str,
        version: str | None = None,
        sig: str | None = None,
        artifact_type: str = "app",
        size: int | None = None,
    ) -> list[dict[str, object]]:
        return run_dock_stage(
            self,
            cmd_id,
            data,
            sha256=sha256,
            version=version,
            sig=sig,
            artifact_type=artifact_type,
            size=size,
        )

    def dock_reboot_staged(self) -> None:
        run_dock_reboot_staged(self)

    def dock_confirm_boot(self) -> list[dict[str, object]]:
        return run_dock_confirm_boot(self)

    def dock_drain(self) -> dict[str, object]:
        return run_dock_drain(self)

    @property
    def boot_ok(self) -> bool:
        return self.pending is None or self.reboot.is_set()

    @property
    def _safe_window_path(self) -> str | None:
        if self.safe_window.startswith(SAFE_WINDOW_FILE_PREFIX):
            return self.safe_window[len(SAFE_WINDOW_FILE_PREFIX) :] or None
        return None

    @property
    def window_can_open(self) -> bool:
        return self._safe_window_path is not None

    def _unsafe_now(self) -> bool:
        if self.safe_window == SAFE_WINDOW_HOLD:
            return True
        path = self._safe_window_path
        if path is not None:
            return not Path(path).exists()
        return False

    def _waits(self, what: str) -> str:
        if self.safe_window == SAFE_WINDOW_HOLD:
            return f"--safe-window hold: the {what} waits, indefinitely"
        return f"--safe-window {self.safe_window}: the {what} waits until the window opens"

    @property
    def _selftest_registered(self) -> bool:
        return CAPABILITY_SELFTEST in self.identity.capabilities

    @property
    def _answers_commands(self) -> bool:
        return any(verb in self.identity.capabilities for verb in VERB_COMMANDS)

    def _fails_selftest(self, version: str) -> bool:
        return self.selftest_fail_marker is not None and self.selftest_fail_marker in version

    def _is_broken(self, version: str) -> bool:
        return self.broken_marker is not None and self.broken_marker in version

    async def handle(self, client: aiomqtt.Client, message: aiomqtt.Message, step: Step) -> None:
        raw = message.payload if isinstance(message.payload, bytes | bytearray) else b""
        try:
            body = json.loads(bytes(raw))
        except (json.JSONDecodeError, UnicodeDecodeError):
            step(f"command  {message.topic.value} ({len(raw)} bytes, undecodable) — ignored")
            return
        if not isinstance(body, dict):
            step(f"command  {message.topic.value} is not a JSON object — ignored")
            return

        command_id = body.get("id")
        if isinstance(command_id, str) and (
            command_id in self.seen or command_id == self.last_reboot_cmd_id
        ):
            step(f"command  {message.topic.value} id={command_id} DUPLICATE — dropped")
            return
        if isinstance(command_id, str):
            self.seen = {command_id}

        kind = body.get("type")
        step(f"command  {message.topic.value} id={command_id} type={kind}")
        step(f"payload  {json.dumps(redacted_command(body), sort_keys=True, ensure_ascii=False)}")

        if not isinstance(command_id, str) or not command_id:
            step(f"command  a {kind} with no id cannot be deduplicated — ignored")
            return
        if kind == COMMAND_STAGE:
            await self._stage(client, command_id, body, step)
            return
        if isinstance(kind, str) and kind in VERB_COMMANDS and kind in self.identity.capabilities:
            await self._verb(client, command_id, kind, body, step)
            return
        if not self._answers_commands:
            step(f"command  type={kind} is not implemented by this board's agent — ignored")
            return
        await self._ack(
            client,
            command_id,
            kind if isinstance(kind, str) else None,
            ACK_REFUSED,
            None,
            ACK_NOT_IMPLEMENTED,
            step,
        )

    async def _verb(
        self,
        client: aiomqtt.Client,
        command_id: str,
        kind: str,
        body: dict[str, object],
        step: Step,
    ) -> None:
        await run_verb(self, client, command_id, kind, body, step)

    async def _apply(
        self, client: aiomqtt.Client, command_id: str, cmd_id: str, step: Step
    ) -> None:
        await run_apply(self, client, command_id, cmd_id, step)

    async def _cancel(
        self, client: aiomqtt.Client, command_id: str, cmd_id: str, step: Step
    ) -> None:
        await run_cancel(self, client, command_id, cmd_id, step)

    async def _rollback_command(self, client: aiomqtt.Client, command_id: str, step: Step) -> None:
        await run_rollback_command(self, client, command_id, step)

    async def _carry_out_rollback(
        self, client: aiomqtt.Client, transaction: str, step: Step
    ) -> None:
        await run_carry_out_rollback(self, client, transaction, step)

    async def _deferred_rollback(
        self, client: aiomqtt.Client, command_id: str, transaction: str, step: Step
    ) -> bool:
        return await run_deferred_rollback(self, client, command_id, transaction, step)

    async def _reboot_command(self, client: aiomqtt.Client, command_id: str, step: Step) -> None:
        await run_reboot_command(self, client, command_id, step)

    async def release_if_safe(self, client: aiomqtt.Client, step: Step) -> None:
        await run_release_if_safe(self, client, step)

    def _clear_staged(self) -> None:
        self.staged = None
        self.staged_version = None
        self.apply_deferred = None

    async def _ack(
        self,
        client: aiomqtt.Client,
        command_id: str,
        kind: str | None,
        result: str,
        cmd_id: str | None,
        detail: str | None,
        step: Step,
    ) -> None:
        payload: dict[str, object] = {
            "id": command_id,
            "type": kind,
            "result": result,
            "cmd_id": cmd_id,
            "detail": detail,
        }
        await _paced_publish(
            client,
            self.link,
            up_topic(self.identity.device_id, ACK),
            encode(payload),
            retain=False,
            step=step,
        )
        step(f"ack      {kind} {result}" + (f" ({detail})" if detail else ""))

    async def _stage(
        self, client: aiomqtt.Client, cmd_id: str, body: dict[str, object], step: Step
    ) -> None:
        if self.staged == cmd_id or (
            (self.pending is not None and self.pending.cmd_id == cmd_id)
            or self.rolled_back == cmd_id
        ):
            step(f"stage    id={cmd_id} already handled by this transaction — ignored")
            return

        artifact = body.get("artifact")
        if not isinstance(artifact, dict):
            await self._status(client, cmd_id, STATE_FAILED, step, detail="no artifact in command")
            return
        url = artifact.get("url")
        expected_sha = artifact.get("sha256")
        version = artifact.get("version")
        sig = artifact.get("sig")
        raw_type = artifact.get("type")
        artifact_type = raw_type if isinstance(raw_type, str) and raw_type else "app"
        if not isinstance(url, str) or not isinstance(expected_sha, str):
            await self._status(
                client, cmd_id, STATE_FAILED, step, detail="artifact url/sha256 missing"
            )
            return
        if not _SHA256_HEX.fullmatch(expected_sha):
            await self._status(
                client, cmd_id, STATE_FAILED, step, detail="artifact sha256 malformed"
            )
            return
        if sig is not None and not _is_valid_artifact_sig(sig):
            await self._status(client, cmd_id, STATE_FAILED, step, detail="artifact sig malformed")
            return

        await self._status(client, cmd_id, STATE_STAGING, step)

        if artifact_type != "app":
            await self._stage_cargo(
                client,
                cmd_id,
                artifact_type=artifact_type,
                url=url,
                expected_sha=expected_sha,
                sig=sig if isinstance(sig, str) else None,
                version=version if isinstance(version, str) and version else None,
                step=step,
            )
            return

        if self.pending is not None:
            step(f"stage    {self.pending.cmd_id} has not confirmed yet — refused, nothing fetched")
            await self._status(
                client, cmd_id, STATE_FAILED, step, detail="the running image is not confirmed yet"
            )
            return
        if self.staged is not None:
            step(
                f"stage    {self.staged} is staged and waits for a reboot — refused, nothing fetched"
            )
            await self._status(
                client,
                cmd_id,
                STATE_FAILED,
                step,
                detail="an update is already staged and waits for a reboot",
            )
            return

        size = artifact.get("size")
        if isinstance(size, int) and size > self.identity.ota_slot_size:
            step(f"stage    {size} bytes will not fit a {self.identity.ota_slot_size}-byte slot")
            await self._status(
                client, cmd_id, STATE_FAILED, step, detail="artifact larger than the ota slot"
            )
            return

        await self._status(client, cmd_id, STATE_DOWNLOADING, step)
        try:
            data = await asyncio.to_thread(
                _resolve_fetch(),
                url,
                max_retries=self.download_retries,
                initial_backoff_s=self.download_backoff_s,
                step=step,
            )
        except OSError as exc:
            await self._status(
                client, cmd_id, STATE_FAILED, step, detail=f"download failed: {type(exc).__name__}"
            )
            return
        self.downloads += 1
        step(f"download {len(data)} bytes from {redact_url(url)} (download #{self.downloads})")

        await self._status(client, cmd_id, STATE_VERIFYING, step)
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha:
            step(f"verify   MISMATCH got {digest}, expected {expected_sha}")
            await self._status(client, cmd_id, STATE_FAILED, step, detail="sha256 mismatch")
            return
        step(f"verify   sha256 {digest} matches")
        if isinstance(sig, str):
            if not _verify_artifact_sig(digest, sig, self.signing_pubkey_ring):
                step("verify   SIGNATURE INVALID against the trusted public key ring")
                await self._status(client, cmd_id, STATE_FAILED, step, detail="signature invalid")
                return
            step("verify   ecdsa-p256-sha256 signature verified")
        elif self.require_signature:
            step("verify   SIGNATURE MISSING (require_signature=True)")
            await self._status(client, cmd_id, STATE_FAILED, step, detail="signature missing")
            return

        await self._status(client, cmd_id, STATE_STAGED, step)

        staged_version = version if isinstance(version, str) and version else None
        if body.get("apply") == APPLY_ON_COMMAND:
            self.staged = cmd_id
            self.staged_version = staged_version
            step("stage    apply=on_command — staying staged until an apply arrives")
            return

        if self._unsafe_now():
            await self._status(client, cmd_id, STATE_AWAITING_SAFE_WINDOW, step)
            self.staged = cmd_id
            self.staged_version = staged_version
            self.apply_deferred = cmd_id
            if self.safe_window == SAFE_WINDOW_HOLD:
                step("stage    --safe-window hold: parked in awaiting_safe_window, indefinitely")
            else:
                step(f"stage    {self._waits('apply')}: parked in awaiting_safe_window")
            return

        await self._release(client, cmd_id, staged_version, step)

    async def _stage_cargo(
        self,
        client: aiomqtt.Client,
        cmd_id: str,
        *,
        artifact_type: str,
        url: str,
        expected_sha: str,
        sig: str | None,
        version: str | None,
        step: Step,
    ) -> None:
        await run_stage_cargo(
            self,
            client,
            cmd_id,
            artifact_type=artifact_type,
            url=url,
            expected_sha=expected_sha,
            sig=sig,
            version=version,
            step=step,
            fetch_fn=_resolve_fetch(),
        )

    async def _release(
        self,
        client: aiomqtt.Client,
        cmd_id: str,
        version: str | None,
        step: Step,
        *,
        note_stage_reboot: bool = True,
    ) -> None:
        await self._status(client, cmd_id, STATE_APPLYING, step)
        await self._status(client, cmd_id, STATE_REBOOTING, step)
        if note_stage_reboot:
            self.last_reboot_cmd_id = cmd_id
        self.pending = PendingConfirm(
            cmd_id=cmd_id, previous_identity=self.identity, previous_slot=self.slot
        )
        self.slot = SLOT_LABELS[1] if self.slot == SLOT_LABELS[0] else SLOT_LABELS[0]
        self._confirm_deadline = None
        self.identity = replace(self.identity, reset_reason=RESET_REASON_SW)
        if version:
            self.identity = replace(self.identity, fw_version=version)
            step(f"apply    now running fw_version {version} (announced on the next connect)")
        self.reboot.set()

    async def on_boot(self, client: aiomqtt.Client, step: Step) -> asyncio.Task[None] | None:
        return await run_on_boot(self, client, step)

    async def _confirm_timeout(
        self, client: aiomqtt.Client, pending: PendingConfirm, delay_s: float, step: Step
    ) -> None:
        await run_confirm_timeout(self, client, pending, delay_s, step)

    async def _rollback(
        self, client: aiomqtt.Client, pending: PendingConfirm, head: str | None, step: Step
    ) -> None:
        await run_rollback(self, client, pending, head, step)

    async def _status(
        self,
        client: aiomqtt.Client,
        cmd_id: str,
        state: str,
        step: Step,
        *,
        pct: int | None = None,
        detail: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "cmd_id": cmd_id,
            "state": state,
            "pct": pct,
            "detail": detail,
        }
        await _paced_publish(
            client,
            self.link,
            up_topic(self.identity.device_id, STATUS),
            encode(payload),
            retain=True,
            step=step,
        )
        step(f"status   {state}")
